"""掃描主控台的端點契約：這裡按下去會花真的錢，所以測的是「有沒有先擋住」。

三段流程（估算 → 確認 → 執行）中間那段花錢，所以每一條測試都在問同一件事：
**使用者授權的，是不是就是實際會發生的那件事。**

離線保證：`offline` fixture 把帳本與任務目錄指到 tmp_path、把 `jobs.spawn`
換成只落地不執行，並把 `scan._place_ids` 換成本機無 place_id。因此沒有任何
Apify／Google／news／PTT 請求會被送出，也不會碰到真實的
`data/runtime/scan/ledger.jsonl`（那裡是實際發生過的花費）。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from smart_watchdog.api import scan
from smart_watchdog.api.server import app
from smart_watchdog.realtime import jobs, ledger, plan, pricing

ROOT = pathlib.Path(__file__).resolve().parents[1]
DISTRICTS = ("板橋區", "新莊區", "三重區")

POINTS = [
    {"i": f"id{k:04d}", "n": f"第{k}", "full": f"新北市私立第{k}幼兒園",
     "d": DISTRICTS[k % 3], "t": 2 if k % 2 else 1, "r": k + 1,
     "cf": 1 if k % 5 == 0 else 0, "ch": 0, "ep": 1 if k % 7 == 0 else 0,
     "fin": 1 if k % 3 else 0}
    for k in range(30)
]


@pytest.fixture
def offline(tmp_path, monkeypatch):
    """把所有會留下痕跡或會連外的東西導開。回傳 tmp 內的路徑。"""
    scan_dir = tmp_path / "runtime"
    monkeypatch.setattr(ledger, "DIR", scan_dir)
    monkeypatch.setattr(ledger, "PATH", scan_dir / "ledger.jsonl")
    monkeypatch.setattr(ledger, "LOCK", scan_dir / "ledger.lock")
    monkeypatch.setattr(jobs, "DIR", scan_dir)
    monkeypatch.setattr(jobs.STORE, "dir", scan_dir)
    assert tmp_path in ledger.PATH.parents, "測試指到了真實帳本"
    assert tmp_path in jobs.STORE.dir.parents, "測試指到了真實任務目錄"

    # 任務只落地、不執行：worker 會真的打 Apify／Google／news／PTT。
    def _no_spawn(job, worker, store=None):
        del worker
        (store or jobs.STORE).write(job)
        return job

    monkeypatch.setattr(jobs, "spawn", _no_spawn)
    # 本機不知道任何 place_id，且不讀 data/processed 的 CSV。
    monkeypatch.setattr(scan, "_place_ids", dict)
    monkeypatch.setitem(scan._ctx, "payload", lambda: {"points": POINTS})
    monkeypatch.setitem(scan._ctx, "proposal",
                        lambda n=20: {"proposal": POINTS[:n]})
    return scan_dir


@pytest.fixture
def client(offline):
    """不用 context manager：startup 會重新 bind 真的 payload 並掃真的任務目錄。"""
    del offline
    return TestClient(app)


def _post(client, path="", **body):
    return client.post(f"/api/scan{path}", json=body)


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_options_lists_every_channel_scope_and_preset(client):
    """主控台的每一個選項都要有來源。少一個管道，操作者就以為那件事沒得做。"""
    o = client.get("/api/scan/options").json()
    assert len(o["channels"]) == 6
    assert len(o["scopes"]) == len(plan.SCOPES) == 7
    assert len(o["keyword_presets"]) == 5
    assert set(o["districts"]) == set(DISTRICTS)


def test_every_channel_says_what_it_costs_and_whether_it_can_go_per_institution(client):
    """成本結構相反的兩種管道共用一個「範圍」控制項，所以每條都要自己講清楚。"""
    chans = {c["key"]: c for c in client.get("/api/scan/options").json()["channels"]}
    for key, c in chans.items():
        assert c["cost_note"].strip(), f"{key} 沒有成本說明"
        assert "status" in c and "legal_basis" in c
    assert chans["apify_threads"]["per_institution_supported"] is False
    assert chans["places_reviews"]["per_institution_supported"] is True


def test_options_explains_why_per_institution_threads_is_unavailable(client):
    """「不提供」要附上算式，否則下一個人會以為只是還沒做。"""
    o = client.get("/api/scan/options").json()
    assert str(pricing.CAP_PER_MONTH_USD) in o["apify_per_institution_blocked"]
    assert o["caps"]["per_institution"] == pricing.PER_INSTITUTION_CAP


def test_estimate_creates_no_job_and_writes_nothing_to_the_ledger(client, offline):
    """估算是免費的那一段。它若留下痕跡，使用者就無法安全地試算。"""
    assert not ledger.PATH.exists()
    r = _post(client, "/estimate", scope="city",
              channels=["apify_threads", "news_rss"], max_posts=50)
    assert r.status_code == 200
    d = r.json()
    assert d["usd_max"] == pricing.apify_meter(50).usd_max
    assert d["confirm_ceiling_usd"] == d["usd_max"]
    assert d["gate"]["ok"] is True

    assert not ledger.PATH.exists(), "估算寫了帳本"
    assert list(offline.glob("*.json")) == [], "估算建立了任務"
    assert jobs.STORE.list() == []


def test_estimate_says_how_few_leads_to_expect(client):
    """上一輪全市掃描 100 篇貼文只歸屬出 1 則。難看但誠實——不講會被當成沒用。"""
    d = _post(client, "/estimate", channels=["apify_threads"],
              max_posts=100).json()
    assert "歷史歸屬率 1–3%" in d["expected_leads"]
    assert d["expires_at"], "估算要有有效期限，否則回押的金額可以無限期使用"


def test_a_stale_ceiling_is_rejected_rather_than_silently_repriced(client):
    """使用者授權的是他看到的那個數字，不是伺服器後來重算出來的別的數字。"""
    r = _post(client, channels=["apify_threads"], max_posts=50,
              confirm_ceiling_usd=0.01)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "PLAN_STALE"
    assert detail["confirmed"] == 0.01
    assert detail["now"] == pricing.apify_meter(50).usd_max
    assert detail["plan"], "拒絕時要附上新計畫，讓使用者能重新授權"
    assert jobs.STORE.list() == [], "被拒絕的請求不該留下任務"


def test_a_matching_ceiling_goes_through(client):
    """回押機制不能連正確的金額都擋掉，否則前端只會學會不要送這個欄位。"""
    r = _post(client, channels=["apify_threads"], max_posts=50,
              confirm_ceiling_usd=pricing.apify_meter(50).usd_max)
    assert r.status_code == 200
    assert r.json()["state"] in jobs.ACTIVE


def test_a_single_apify_run_can_no_longer_reach_the_run_cap(client):
    """max_posts 夾到 actor 上限之後，單次 Apify 執行最多 US$0.33。

    這是 `clamp_posts` 的副作用，而且是想要的：估算只能算出實際可能發生的
    金額。max_posts=1000 曾估成 US$2.58 並被單次上限擋下——使用者被告知
    不能做一件其實只花 US$0.33 的事。
    """
    r = _post(client, channels=["apify_threads"], max_posts=1000)
    assert r.status_code == 200
    assert pricing.apify_meter(1000).usd_max == pricing.apify_meter(
        pricing.APIFY_MAX_POSTS).usd_max < pricing.CAP_PER_RUN_USD


def test_over_the_run_cap_is_429_and_names_the_cap(client, monkeypatch):
    """被擋下來時要說出是哪一道牆，否則操作者只會一直重按。

    單次上限現在只有逐園管道撞得到：免費額度用罄後 25 家 Google 評論查詢
    是 US$0.625。用它來驗證這道牆，而不是用一個估算已經夾掉的數字。
    """
    ledger.note_call("places_reviews", 1000, detail="用罄本月免費額度")
    # fixture 讓 _place_ids 回空字典以保持離線；這個情境需要有 place_id 的園，
    # 否則先撞到「無任何一家有 place_id」的 blocker（400），走不到預算檢查。
    ids = [p["i"] for p in POINTS[:pricing.PER_INSTITUTION_CAP]]
    monkeypatch.setattr(scan, "_place_ids", lambda: {i: f"place_{i}" for i in ids})
    r = _post(client, scope="picked", ids=ids, channels=["places_reviews"])
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert detail["error"] == "OVER_CAP"
    assert detail["cap"] == "run"
    assert detail["reason"] and detail["budget"]["source"]


def test_over_the_day_cap_is_also_429_with_the_day_named(client):
    """單次上限之內的掃描連按幾次也會撞牆，而那是不同的一道牆。"""
    ledger.reserve(0.95, "apify_threads", "earlier_today")
    r = _post(client, channels=["apify_threads"], max_posts=50)
    assert r.status_code == 429
    assert r.json()["detail"]["cap"] == "day"


def test_a_blocked_plan_is_400_before_any_budget_check(client):
    """被擋的計畫不會執行，所以它連額度檢查都不該走到——先把原因講出來。"""
    r = _post(client, scope="picked", ids=[POINTS[0]["i"]],
              channels=["places_reviews"])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "BLOCKED"
    assert detail["blockers"] and "place_id" in detail["blockers"][0]
    assert jobs.STORE.list() == []


def test_double_click_does_not_become_two_billable_runs(client):
    """同一組參數連按兩下，第二次必須認出第一次，而不是再跑一次。"""
    body = {"scope": "city", "channels": ["apify_threads"], "max_posts": 50}
    first = _post(client, **body).json()
    second = _post(client, **body).json()
    assert second["job_id"] == first["job_id"]
    assert second["deduplicated"] is True
    assert len(jobs.STORE.list()) == 1

    # 參數不同就是另一次掃描，不可被去重吃掉。
    third = _post(client, **{**body, "max_posts": 60}).json()
    assert third["job_id"] != first["job_id"]


def test_a_started_job_is_readable_and_reports_the_full_attribution_pool(client):
    """任務摘要要能回答「這次掃了誰、拿去跟誰比對」，後者恆為全部機構。"""
    job_id = _post(client, scope="picked", ids=[POINTS[0]["i"]],
                   channels=["news_rss"]).json()["job_id"]
    j = client.get(f"/api/scan/jobs/{job_id}").json()
    assert j["attribution_pool"] == len(POINTS)
    assert j["data_completeness"] == "unusable", "還沒跑完不得顯示為完整"
    assert client.get("/api/scan/jobs/deadbeef0000").status_code == 404


def test_every_job_is_pending_human_review(client):
    """掃描結果是候選線索，不是判定。這個欄位不會有別的值。"""
    for channels in (["news_rss"], ["apify_threads"], ["news_rss", "ptt"]):
        j = _post(client, channels=channels, max_posts=50).json()
        assert j["disposition"] == "待人工研判"
        assert "不進入分數" in j["note"]
        assert "risk" not in j and "score" not in j


def test_adopting_a_lead_changes_no_scored_artefact(client, offline):
    """掃描結果不進分數是架構保證，不是約定——沒有那條程式路徑。

    採用只是「這條值得看」。它必須寫進 data/runtime/，而
    audit_priority_ntpc.csv 與 payload.json 一個位元都不能動。
    """
    csv_path = ROOT / "data/processed/audit_priority_ntpc.csv"
    payload_path = ROOT / "dist/data/payload.json"
    if not (csv_path.exists() and payload_path.exists()):
        pytest.skip("尚未執行分析管線／build_frontend.py")
    before = (_sha(csv_path), _sha(payload_path))

    job = jobs.new_job(
        plan.build_plan(POINTS, scope="city", channels=["news_rss"]).as_dict(),
        {"scope": "city"})
    job["mentions"] = [{"url": "https://example.invalid/a", "channel": "news_rss",
                        "institution_id": POINTS[0]["i"], "headline": "標題"},
                       {"url": "https://example.invalid/b", "channel": "news_rss",
                        "institution_id": POINTS[1]["i"], "headline": "另一則"}]
    jobs.STORE.write(job)

    r = client.post(f"/api/scan/jobs/{job['job_id']}/adopt",
                    json={"urls": ["https://example.invalid/a"], "reviewer": "甲"})
    assert r.status_code == 200
    body = r.json()
    assert body["adopted"] == 1
    assert "不進入分數" in body["note"]

    adopted = offline / "adopted.jsonl"
    assert adopted.exists(), "採用的線索必須落在 data/runtime/ 之下"
    rec = json.loads(adopted.read_text(encoding="utf-8").splitlines()[0])
    assert rec["status"] == "待人工研判"
    assert rec["reviewer"] == "甲" and rec["job_id"] == job["job_id"]

    assert (_sha(csv_path), _sha(payload_path)) == before, "掃描結果進了分數"


def test_budget_endpoint_reports_the_caps_and_the_apify_cycle(client):
    """畫面上的「本月剩餘」必須說清楚那個月是誰的月——Apify 的月從 8 日開始。"""
    b = client.get("/api/scan/budget").json()
    assert b["caps"] == {"run": pricing.CAP_PER_RUN_USD,
                         "day": pricing.CAP_PER_DAY_USD,
                         "month": pricing.CAP_PER_MONTH_USD}
    assert b["cycle_start"].endswith("-08"), "訂閱週期起日是每月 8 日"
    assert "本機帳本" in b["source"]


def test_ledger_endpoint_shows_the_reservations_that_occupy_budget(client):
    """未結算的預留必須看得見，否則「錢不知道去哪了」只能靠猜。"""
    ledger.reserve(0.205, "apify_threads", "job1", detail="sweep")
    entries = client.get("/api/scan/ledger").json()["entries"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "reserve" and entries[0]["status"] == "unknown"


def test_a_channel_that_did_not_succeed_must_say_why():
    """PTT 與 news 被限流時的表徵是空結果不是錯誤。

    把 failed 顯示成「這些園沒人在談」，是把系統故障呈現成稽查結論。所以非 ok
    的結局在型別層就要求原因，寫不出原因的程式路徑根本無法構造出這個物件。
    """
    for outcome in (jobs.EMPTY, jobs.PARTIAL, jobs.FAILED_CH, jobs.SKIPPED,
                    jobs.BLOCKED):
        with pytest.raises(ValueError):
            jobs.ChannelOutcome("news_rss", "新聞", outcome)
    ok = jobs.ChannelOutcome("news_rss", "新聞", jobs.OK, mentions=1)
    assert ok.reason == ""
    assert jobs.ChannelOutcome("ptt", "PTT", jobs.EMPTY,
                               reason="本次無可歸屬內容").reason
