"""Apify 上的 Threads 資料管道。

官方 Threads API 的 `threads_keyword_search` 需 App Review（2–4 週），決賽前
拿不到。Apify 的 actor 當天訂閱即可用，是實務上唯一趕得上的 Threads 來源。

**成本決定了呼叫方式。** 計價是 PAY_PER_EVENT，不是每次執行固定金額：
啟動費 $0.02/GB（FREE）× 4GB = $0.08，**再加每筆回傳資料 $0.0025**。
一次回傳 100 筆的掃描實付 US$0.33，不是 US$0.08。公式與對帳見
``pricing.py``——那裡是唯一的價目表，這個模組不自己算錢。

* ``sweep()`` 是主要用法——一次查「幼兒園」這類廣詞，本地對 1,213 園做歸屬。
  一次執行覆蓋全市，成本與範圍無關。
* ``search()`` 逐園查詢預設關閉（``allow_per_institution=False``）。
  50 家 × US$0.205 = US$10.25，一次耗盡整月額度還不夠。

**執行改成先建立再輪詢，不用 run-sync。** run-sync 回傳的是資料，不回傳
``run_id``——當機或逾時就再也查不到那次執行實際花了多少，帳本會永遠停在
預估值。``start_run()`` 讓 run_id 在等待之前就落地，``poll_run()`` 讀回
供應商自報的 ``usageTotalUsd`` 供結算。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, ClassVar

from .sources import LIVE, NEEDS_APPROVAL, Channel, Mention

API = "https://api.apify.com/v2"
# 繁中專用的 actor：輸入接受 keywords 陣列，輸出欄位為 text_content/post_url/
# created_at/username，語言與我們的語料一致。
DEFAULT_ACTOR = "futurizerush~meta-threads-scraper-zh-tw"
#: 啟動費地板（4GB × $0.02）。**不是**一次執行的總價——每筆回傳另計 $0.0025。
#: 保留這個名稱是為了不破壞既有匯入；要算錢請用 pricing.apify_meter()。
COST_PER_RUN_USD = 0.08
#: Apify 的終局狀態；輪詢看到這些就停。
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})
POLL_SECONDS = 3.0


class ApifyThreadsChannel(Channel):
    """Threads 貼文，經由 Apify actor 取得。"""

    key = "apify_threads"
    label = "Threads（Apify）"
    legal_basis = "由 Apify 上的第三方 actor 供應，依執行次數與筆數計價"
    status = NEEDS_APPROVAL
    may_store = True
    how_to_enable = "在 .env 設定 APIFY_TOKEN；可另以 APIFY_THREADS_ACTOR 指定 actor"

    #: actor 輸出的欄位名稱。換 actor 時只需改這張表。
    FIELDS: ClassVar[dict[str, tuple[str, ...]]] = {
        "text": ("text_content", "text", "caption", "content"),
        "url": ("post_url", "url", "permalink", "link"),
        "date": ("created_at", "taken_at", "timestamp", "published_on"),
        "author": ("username", "display_name", "author", "user"),
    }

    def __init__(self, token: str | None = None, actor: str = DEFAULT_ACTOR,
                 *, allow_per_institution: bool = False,
                 timeout: int = 280) -> None:
        self.token = token
        self.actor = actor
        self.allow_per_institution = allow_per_institution
        self.timeout = timeout
        self.runs_used = 0
        self.items_returned = 0
        self.plan: str | None = None
        self.last_run_id: str | None = None
        self._charged: list[float] = []
        if token:
            self.status = LIVE

    # ── 成本 ────────────────────────────────────────────────
    @property
    def estimated_cost_usd(self) -> float:
        """已發生的成本。用實際回傳筆數，不是每次固定 US$0.08。

        先前這裡是 ``runs_used × 0.08``，對一次 100 筆的掃描少報 4.1 倍
        （實付 US$0.33）。單價與公式集中在 ``pricing.py``。
        """
        from . import pricing

        start = pricing.apify_start_usd(self.plan)
        return round(self.runs_used * start
                     + self.items_returned * pricing.APIFY_ITEM_USD, 4)

    @property
    def charged_usd(self) -> float | None:
        """供應商自報的累計金額；沒有就回 None，不要拿估算冒充實付。"""
        return round(sum(self._charged), 4) if self._charged else None

    def account(self) -> dict[str, Any]:
        """目前方案與剩餘額度，讓呼叫端能在跑之前決定要不要跑。"""
        if not self.token:
            return {}
        try:
            data = self._get(f"{API}/users/me").get("data") or {}
        except OSError:
            return {}
        plan = data.get("plan") or {}
        self.plan = plan.get("id") or self.plan
        return {"username": data.get("username"), "plan": plan.get("id"),
                "monthly_credits_usd": plan.get("monthlyUsageCreditsUsd")}

    def provider_usage(self) -> dict[str, Any]:
        """供應商端的本週期用量——比我方帳本權威，用來校正。"""
        if not self.token:
            return {}
        try:
            cur = (self._get(f"{API}/users/me/limits").get("data")
                   or {}).get("current") or {}
        except (OSError, ValueError):
            return {}
        return {"monthly_usage_usd": cur.get("monthlyUsageUsd")}

    # ── HTTP ───────────────────────────────────────────────
    def _get(self, url: str) -> dict:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read(8_000_000))

    def start_run(self, keywords: list[str], limit: int,
                  search_filter: str = "top", *,
                  max_charge_usd: float | None = None,
                  on_started: Any = None) -> str | None:
        """建立一次執行，回傳 run_id。**不等結果。**

        ``on_started`` 在 run_id 到手的當下、開始等待之前被呼叫一次，讓呼叫端
        先把 run_id 落地。當機時那筆錢的真實金額還在變大，能查回來才對得了帳。

        ``max_charge_usd`` 送進 Apify 的 ``maxTotalChargeUsd``——這是唯一由
        供應商自己執行的硬上限。我方三道上限是我方程式在擋，這一道不是。
        """
        body: dict[str, Any] = {
            "mode": "keyword", "keywords": keywords,
            "max_posts": max(1, min(limit, 100)),
            "search_filter": search_filter,
        }
        params = {}
        if max_charge_usd is not None:
            params["maxTotalChargeUsd"] = f"{max(0.005, max_charge_usd):.4f}"
        url = (f"{API}/acts/{urllib.parse.quote(self.actor)}/runs"
               + (f"?{urllib.parse.urlencode(params)}" if params else ""))
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = (json.loads(r.read(2_000_000)).get("data") or {})
        except (urllib.error.HTTPError, OSError, ValueError):
            return None
        run_id = data.get("id")
        if run_id:
            self.runs_used += 1
            self.last_run_id = run_id
            if on_started:
                on_started(run_id, data.get("defaultDatasetId"))
        return run_id

    def poll_run(self, run_id: str) -> dict[str, Any]:
        """一次狀態查詢。回傳 status / usageTotalUsd / defaultDatasetId。"""
        try:
            d = self._get(f"{API}/actor-runs/{run_id}").get("data") or {}
        except (OSError, ValueError):
            return {}
        return {"status": d.get("status"), "dataset_id": d.get("defaultDatasetId"),
                "usage_usd": d.get("usageTotalUsd"),
                "charged": d.get("chargedEventCounts") or {}}

    def fetch_items(self, dataset_id: str, limit: int = 200) -> list[dict]:
        try:
            data = self._get(f"{API}/datasets/{dataset_id}/items?limit={limit}")
        except (OSError, ValueError):
            return []
        rows = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
        self.items_returned += len(rows)
        return rows

    def abort_run(self, run_id: str) -> bool:
        req = urllib.request.Request(
            f"{API}/actor-runs/{run_id}/abort", data=b"",
            headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except (urllib.error.HTTPError, OSError):
            return False

    def _run(self, keywords: list[str], limit: int,
             search_filter: str = "top", *,
             max_charge_usd: float | None = None,
             on_started: Any = None) -> list[dict]:
        """建立執行、輪詢到結束、取回資料。失敗回空清單而不是讓面板掛掉。"""
        run_id = self.start_run(keywords, limit, search_filter,
                                max_charge_usd=max_charge_usd,
                                on_started=on_started)
        if not run_id:
            return []
        deadline = time.time() + self.timeout
        info: dict[str, Any] = {}
        while time.time() < deadline:
            info = self.poll_run(run_id)
            if info.get("status") in TERMINAL:
                break
            time.sleep(POLL_SECONDS)
        if info.get("usage_usd") is not None:
            self._charged.append(float(info["usage_usd"]))
        dataset = info.get("dataset_id")
        return self.fetch_items(dataset, limit=max(1, min(limit, 100))) if dataset else []

    def _pick(self, row: dict, field: str) -> str:
        for name in self.FIELDS[field]:
            value = row.get(name)
            if value:
                return str(value)
        return ""

    def _to_mentions(self, rows: list[dict], institutions: list[dict],
                     limit: int) -> list[Mention]:
        from ..features.alerts import article_kind, attribute

        out: list[Mention] = []
        seen: set[str] = set()
        for row in rows:
            if len(out) >= limit:
                break
            text = self._pick(row, "text")
            url = self._pick(row, "url")
            if not text or url in seen:
                continue
            seen.add(url)
            att = attribute(text, institutions)
            if not att.attributed:
                continue
            author = self._pick(row, "author")
            out.append(Mention(
                channel=self.key, institution_id=att.institution_id,
                headline=f"[{article_kind(text)}] {text[:180]}",
                url=url, published=self._pick(row, "date")[:10],
                publisher=f"Threads @{author}" if author else "Threads",
                attribution_basis=att.basis))
        return out

    # ── Channel ────────────────────────────────────────────
    def search(self, institution: dict, limit: int = 20) -> list[Mention]:
        """單園查詢。預設關閉——每次呼叫都是一次計費執行。"""
        if not self.token or not self.allow_per_institution:
            return []
        from ..features.alerts import distinctive_name

        core = distinctive_name(str(institution.get("title", "")))
        if len(core) < 2:
            return []
        rows = self._run([f"{core}幼兒園"], limit)
        return self._to_mentions(rows, [institution], limit)

    def sweep(self, institutions: list[dict], limit: int = 200, *,
              keywords: list[str] | None = None,
              search_filter: str = "recent",
              max_charge_usd: float | None = None,
              on_started: Any = None) -> list[Mention]:
        """全市掃描：一次執行覆蓋 1,213 園，這是本管道的主要用法。

        ``institutions`` 必須是**全部** 1,213 園，不是掃描範圍——歸屬要看得見
        所有機構才有辦法在名稱可對應多家時拒絕，只餵子集會製造誤判。
        """
        if not self.token:
            return []
        rows = self._run(list(keywords or ["幼兒園"]), limit,
                         search_filter=search_filter,
                         max_charge_usd=max_charge_usd, on_started=on_started)
        return self._to_mentions(rows, institutions, limit)
