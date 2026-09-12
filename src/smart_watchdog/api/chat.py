"""自然語言查詢：把問題變成對已算好的資料的檢索，而不是變成新的判斷。

這是 Bedrock 的第四個落點（前三個是財報視覺抽取、稽查建議書生成、以及未來的
輿情分類），但它的設計限制比前三個更嚴：**模型只負責把問題翻成檢索條件，
不負責回答風險問題本身**。

理由和 `report/backends.py` 一樣，但更尖銳。一個會「回答」的聊天介面很容易
被問出這種句子：

    「三重區哪幾間幼兒園比較危險？」

而任何直接回答它的系統都已經違反了本專案的輸出定位——我們產出的是建議查核
的優先序，不是危險認定。所以這裡的模型看不到「請判斷」這種任務：它只把自然
語言映射到 `filter` 與 `sort`，實際的數字由既有資料表回答，措辭由程式組裝。

沒有 Bedrock 也能運作。`KeywordPlanner` 用關鍵字把常見問法映射到同一組檢索
條件，決賽當天換成 `BedrockPlanner` 只是換一個 planner，**能問什麼、答案怎麼
措辭都不變**。
"""

from __future__ import annotations

import abc
import os
import re
import typing
from typing import Any

MAX_ROWS = 12

#: 一次回應最多幾列，不論 planner 要求多少。
#: KeywordPlanner 自己就把 limit 夾在 50，所以這個上限長期沒有被碰到；
#: BedrockPlanner 的 schema 對 limit 沒有上限，實測回過 limit=100，
#: 於是「摘要說幾所」與「實際回幾列」第一次出現分歧。兩處必須共用同一個值。
HARD_LIMIT = 50

TYPE_NAMES = {0: "公立", 1: "非營利", 2: "私立"}
TYPE_INDEX = {"公立": 0, "非營利": 1, "私立": 2, "私幼": 2, "公幼": 0}


class QueryPlan(dict):
    """A retrieval plan: filters plus an ordering. Never a conclusion."""


class Planner(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def plan(self, question: str) -> QueryPlan:
        """Map a question onto filters over the already-computed tables."""


class KeywordPlanner(Planner):
    """規則式對應。無需外部服務，決賽當天是 Bedrock 失效時的地板。"""

    name = "keyword"

    def plan(self, question: str) -> QueryPlan:
        q = str(question)
        plan = QueryPlan(filters={}, sort="rank", limit=MAX_ROWS, intent="list")

        town = re.search(r"([一-鿿]{1,3})區", q)
        if town:
            plan["filters"]["town"] = town.group(0)
        for name, idx in TYPE_INDEX.items():
            if name in q:
                plan["filters"]["type"] = idx
                break

        # 否定先判。「哪些園沒有公開財報」若先命中「財報」會被判成
        # has_compliance_failure——意思正好相反。中間可夾字（沒有*公開*財報），
        # 所以用 regex 而非固定字串。
        no_fin = re.search(r"(沒有|無|未|查不到|沒公告|未公告).{0,4}(財報|財務報告)", q)
        if no_fin:
            plan["filters"]["no_financial"] = True
        elif any(k in q for k in ("財報", "財務", "法遵", "準備金", "專戶")):
            plan["filters"]["has_compliance_failure"] = True
        if any(k in q for k in ("評鑑", "指標")):
            plan["filters"]["evaluation_partial"] = True
        if any(k in q for k in ("裁罰", "罰過", "前科", "違規紀錄")):
            plan["filters"]["has_penalty"] = True
        if any(k in q for k in ("新聞", "輿情", "社群", "討論", "聲音")):
            plan["filters"]["has_mentions"] = True

        if any(k in q for k in ("幾間", "幾家", "多少", "數量", "統計")):
            plan["intent"] = "count"
        n = re.search(r"(?:前|top)\s*(\d{1,3})", q, re.I)
        if n:
            plan["limit"] = min(int(n.group(1)), 50)
        return plan


class BedrockPlanner(Planner):
    """決賽交付版。模型只輸出檢索計畫，不輸出結論。"""

    name = "bedrock"

    SCHEMA: typing.ClassVar[dict] = {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "filters": {
                    "type": "object",
                    "properties": {
                        "town": {"type": "string"},
                        "type": {"type": "integer"},
                        "has_compliance_failure": {"type": "boolean"},
                        "evaluation_partial": {"type": "boolean"},
                        "has_penalty": {"type": "boolean"},
                        "has_mentions": {"type": "boolean"},
                        "no_financial": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "sort": {"type": "string", "enum": ["rank", "penalties", "recent"]},
                "limit": {"type": "integer"},
                "intent": {"type": "string", "enum": ["list", "count"]},
            },
            "required": ["filters", "sort", "limit", "intent"],
            "additionalProperties": False,
        },
    }

    PROMPT = """你是一個檢索計畫產生器。把使用者的中文問題翻譯成檢索條件。

**你不回答問題，也不做任何判斷。** 你只輸出 filters／sort／limit／intent。
實際的數字由系統從已計算好的資料表取得，措辭由程式組裝。

可用的 filters：
- town：行政區，例如「三重區」
- type：0=公立 1=非營利 2=私立
- has_compliance_failure：財務報告法遵檢核未通過
- evaluation_partial：近兩年官方評鑑有指標未通過
- has_penalty：有歷史裁罰紀錄
- has_mentions：近期有可歸屬的公開報導
- no_financial：無公開財務報告

若問題涉及「哪間比較危險／有問題／會出事」這類判斷，仍只輸出檢索條件
（通常是 sort=rank），因為系統輸出的是建議查核的優先序，不是危險認定。"""

    # 這個落點只做意圖解析（問句 → 檢索條件），延遲比深度重要，所以用最快的
    # 那一個。ID 與 region 由 ..bedrock 統一提供。
    def __init__(self, model: str | None = None,
                 region: str | None = None) -> None:
        from .. import bedrock as _bedrock

        self.model = model or _bedrock.FAST_MODEL
        self.region = region or _bedrock.region()
        self._client = None

    def plan(self, question: str) -> QueryPlan:
        import json

        if self._client is None:
            from .. import bedrock as _bedrock

            # 短逾時、少重試：這個落點只是把問句翻成檢索條件，慢到某個程度
            # 就該直接降級成 KeywordPlanner。SDK 預設 600 秒＋2 次重試，
            # 在會場「連得上但不回應」的網路下會把 sync 端點的 worker 掛住，
            # 前端連降級訊息都收不到——那比回一個錯誤更糟。
            self._client = _bedrock.client(
                self.region, timeout=_bedrock.TIMEOUT_FAST, max_retries=1)
        resp = self._client.messages.create(
            model=self.model, max_tokens=400, system=self.PROMPT,
            messages=[{"role": "user", "content": question}],
            output_config={"format": self.SCHEMA},
        )
        return QueryPlan(json.loads(resp.content[0].text))


def get_planner(kind: str = "keyword", **kwargs) -> Planner:
    if kind == "keyword":
        return KeywordPlanner()
    if kind == "bedrock":
        return BedrockPlanner(**kwargs)
    raise ValueError(f"未知的 planner：{kind}（可用：keyword, bedrock）")


# ── 該用哪一個 planner ───────────────────────────────────────────────
#: 環境變數。`auto`（預設）＝有 Bedrock 就用 Bedrock，沒有就用關鍵字。
#: 另兩個值 `bedrock`／`keyword` 是強制指定，用於「我就是要測那一條路」。
PLANNER_ENV = "CHAT_PLANNER"


def bedrock_available() -> bool:
    """Bedrock 這條路現在走不走得通。

    **只看本機條件，不打網路。** 這個函式會在每次請求時被呼叫，若它自己去
    call Bedrock，聊天介面的延遲就會多一趟往返，而且會場斷網時每一次查詢都
    要先等一個 timeout 才降級——降級必須是免費的，否則等於沒降級。

    真正打不打得通只有送出請求才知道，所以呼叫端仍必須處理例外
    （見 ``api/server.py`` 的 ``/api/chat``）。
    """
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    from .. import bedrock as _bedrock

    _bedrock.load_env()
    return _bedrock.credentials_present()


#: `resolve_kind` 允許回傳的值。錯字必須當場被擋，不能一路帶到 `/api/health`。
KINDS = ("bedrock", "keyword")


def resolve_kind(kind: str | None = None) -> str:
    """把 `auto` 解析成實際要用的 planner 名稱。

    每次請求都重新解析，所以**啟動時沒有憑證、之後才補上 `.env`** 的情況不必
    重啟就會從 keyword 翻成 bedrock。

    ⚠️ **反方向不成立：換掉一把已經載入過的金鑰一定要重啟。**
    `bedrock.load_env()` 不覆寫既有的環境變數，botocore 解析一次後會快取靜態
    憑證，anthropic 1.5.0 對 session 還加了 `lru_cache`。臨時憑證過期後把新的
    四行貼進 `.env`，這個行程仍然拿著舊的——而 `credentials_present()` 只看
    key 存不存在，過期的舊 key 依然算存在，所以這裡照樣回 `bedrock`。
    症狀是每一題都降級並附上「AWS 臨時憑證已過期」。**重啟 `run.py serve`。**

    無法辨識的值退回 `keyword` 而不是拋例外：打錯一個字不該讓聊天整個掛掉，
    但也不能默默照用——`/api/health` 會把它原樣顯示成 planner，在台上掃一眼
    `bedrok` 和 `bedrock` 分不出來。
    """
    raw = (kind or os.environ.get(PLANNER_ENV) or "auto").strip().lower()
    if raw == "auto":
        return "bedrock" if bedrock_available() else "keyword"
    if raw not in KINDS:
        return "keyword"
    return raw


def invalid_kind(kind: str | None = None) -> str:
    """若 `CHAT_PLANNER` 設了一個無法辨識的值，回傳它；否則回空字串。

    給 `/api/health` 用——退回 keyword 這件事必須說得出原因，
    否則設定打錯字會表現成「Bedrock 莫名其妙沒被用到」。
    """
    raw = (kind or os.environ.get(PLANNER_ENV) or "auto").strip().lower()
    return "" if raw == "auto" or raw in KINDS else raw


def resolve_planner(kind: str | None = None, **kwargs) -> Planner:
    """`resolve_kind` 加上建構。呼叫端若要快取實例，用 `resolve_kind` 當 key。"""
    return get_planner(resolve_kind(kind), **kwargs)


def _matches(p: dict, f: dict, mentions: dict) -> bool:
    # 名稱是子字串比對，全名與簡稱都算。使用者與模型講的都是「安溪」，
    # 而主檔存的是「新北市安溪非營利幼兒園(委託社團法人桃園市教保服務人員協會辦理)」。
    if "name" in f:
        q = f["name"]
        if q not in p.get("full", "") and q not in p.get("n", ""):
            return False
    if "town" in f and p["d"] != f["town"]:
        return False
    if "type" in f and p["t"] != f["type"]:
        return False
    if f.get("has_compliance_failure") and not p.get("cf"):
        return False
    if f.get("evaluation_partial") and not p.get("ep"):
        return False
    if f.get("has_penalty") and not p.get("np"):
        return False
    if f.get("no_financial") and p.get("fin"):
        return False
    return not (f.get("has_mentions") and not mentions.get(p["i"]))


def answer(question: str, payload: dict[str, Any], *,
           institution_id: str | None = None,
           planner: Planner | None = None) -> dict[str, Any]:
    """回答一個問題：檢索既有資料，組裝措辭，附上每一列的列入理由。"""
    planner = planner or get_planner("keyword")
    plan = planner.plan(question)
    f = plan.get("filters", {})
    mentions = payload.get("realtime", {}).get("by_institution", {})
    points = payload.get("points", [])

    if institution_id:
        f = {**f}
        points = [p for p in points if p["i"] == institution_id]

    hits = [p for p in points if _matches(p, f, mentions)]
    if plan.get("sort") == "penalties":
        hits.sort(key=lambda p: -p.get("np", 0))
    elif plan.get("sort") == "recent":
        hits.sort(key=lambda p: p.get("evd", ""), reverse=True)
    else:
        hits.sort(key=lambda p: p["r"])

    limit = max(0, min(int(plan.get("limit", MAX_ROWS)), HARD_LIMIT))
    rows = [{
        "id": p["i"], "title": p["full"], "type": TYPE_NAMES.get(p["t"], "?"),
        "town": p["d"], "rank": p["r"],
        "why": p.get("why", ""),
        "compliance_failed": p.get("cf", 0),
        "penalties": p.get("np", 0),
        "financial_data": bool(p.get("fin")),
        "mentions": len(mentions.get(p["i"], [])),
    } for p in hits[:limit]]

    return {
        "question": question,
        "planner": planner.name,
        "plan": dict(plan),
        "matched": len(hits),
        "returned": len(rows),
        "results": rows,
        "summary": _summarise(f, hits, plan, limit),
        # Every answer carries the same caveat the ranking carries. A chat
        # interface is where "哪間比較危險" gets asked, and the reply must not
        # let a retrieval result read as a finding of wrongdoing.
        "caveat": (
            "本結果為依公開資料計算之**建議查核優先序**，非危險或違法認定。"
            "全市 94.8% 的園無公開財務報告，未出現財務發現者屬資料不足，非低風險。"
        ),
    }


def _summarise(f: dict, hits: list[dict], plan: dict, limit: int) -> str:
    """`limit` 由呼叫端算好傳進來，不在這裡重算。

    這兩處曾各自算過一次，於是摘要寫「以下為前 70 所」而實際只回了 50 列。
    使用者看得到的句子必須描述使用者實際拿到的東西。
    """
    scope = []
    if "town" in f:
        scope.append(f["town"])
    if "type" in f:
        scope.append(TYPE_NAMES.get(f["type"], ""))
    where = "".join(scope) or "全市"

    conds = []
    if f.get("has_compliance_failure"):
        conds.append("財報法遵檢核未通過")
    if f.get("evaluation_partial"):
        conds.append("近兩年評鑑有指標未通過")
    if f.get("has_penalty"):
        conds.append("有歷史裁罰紀錄")
    if f.get("has_mentions"):
        conds.append("近期有可歸屬的公開報導")
    if f.get("no_financial"):
        conds.append("無公開財務報告")
    cond = "、".join(conds)

    if plan.get("intent") == "count":
        return f"{where}符合{('「' + cond + '」') if cond else '條件'}的共 {len(hits)} 所。"
    if not hits:
        return f"{where}沒有符合{('「' + cond + '」') if cond else '條件'}的機構。"
    shown = min(len(hits), limit)
    head = f"{where}{('中' + cond + '的') if cond else ''}共 {len(hits)} 所，"
    return head + f"以稽查優先序排列，以下為前 {shown} 所。"
