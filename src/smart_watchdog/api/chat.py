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
import re
import typing
from typing import Any

MAX_ROWS = 12

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

            self._client = _bedrock.client(self.region)
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


def _matches(p: dict, f: dict, mentions: dict) -> bool:
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

    limit = min(int(plan.get("limit", MAX_ROWS)), 50)
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
        "summary": _summarise(f, hits, plan),
        # Every answer carries the same caveat the ranking carries. A chat
        # interface is where "哪間比較危險" gets asked, and the reply must not
        # let a retrieval result read as a finding of wrongdoing.
        "caveat": (
            "本結果為依公開資料計算之**建議查核優先序**，非危險或違法認定。"
            "全市 94.8% 的園無公開財務報告，未出現財務發現者屬資料不足，非低風險。"
        ),
    }


def _summarise(f: dict, hits: list[dict], plan: dict) -> str:
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
    shown = min(len(hits), int(plan.get("limit", MAX_ROWS)))
    head = f"{where}{('中' + cond + '的') if cond else ''}共 {len(hits)} 所，"
    return head + f"以稽查優先序排列，以下為前 {shown} 所。"
