"""掃描計畫：把「掃哪些園、用哪些管道」換算成請求數與金額上界。

純函式，不發任何請求、不寫任何檔。主控台與排程腳本共用這裡，因此「一次掃描
要花多少錢」不可能有兩個答案。

兩個容易做錯的地方，用型別擋住：

**``target_ids`` 與 ``attribution_ids`` 是兩個欄位。** 前者是「要去查誰」，
後者是「查回來的內容拿去跟誰比對」。後者恆為全部 1,213 園，**不隨範圍縮小**。
``alerts.attribute()`` 的「名稱可對應到 ≥2 所機構就拒絕歸屬」只有在看得見
全部機構時才成立；只餵子集不是少看見幾筆，是製造誤判——把「板橋幼兒園」
歸給名單內那一家，而真正被談論的是名單外的另一家。

**廣掃與逐園的成本結構相反。** Threads 是關鍵字查詢，一次執行覆蓋全市，
成本與範圍**無關**；Google 評論是逐園查詢，成本與範圍**成正比**。同一個
「範圍」控制項在兩種管道下意義不同，估算必須分開算，UI 也必須分開講。
"""

from __future__ import annotations

import dataclasses

from . import pricing

#: 範圍選項。每一個都對應稽查員的一個真實問題。
SCOPES = (
    ("city", "全市 1,213 園", "不過濾。廣掃的預設——範圍外的命中正是最有價值的產出。"),
    ("proposal", "本批派工提案", "送出派工單前的最後一道確認。"),
    ("compliance_fail", "財報法遵未通過", "唯一能把帳面發現與公開討論雙邊佐證的子集。"),
    ("evaluation", "近兩年評鑑部分指標未通過", "中位提前 268 天的官方訊號。"),
    ("top_risk", "分數前 N 名", "分數受「有無公開財報」支配，拿掃描去印證分數有迴音室風險。"),
    ("district", "指定行政區", "分區責任制的稽查員只管自己的區。"),
    ("picked", "指定機構", "單案深掘。"),
)

#: 預設關鍵字。Threads 上真正會出現的家長用語，不是法規術語。
DEFAULT_KEYWORDS = ("幼兒園",)
KEYWORD_PRESETS = {
    "broad": (("幼兒園",), "廣詞，一次執行覆蓋全市，歸屬率低但不漏"),
    "incident": (("幼兒園 不當管教", "幼兒園 體罰", "托嬰 虐待"), "疑似事件用語"),
    "care": (("幼兒園 餵藥", "幼兒園 受傷", "幼兒園 監視器"), "照顧與安全爭議"),
    "money": (("幼兒園 退費", "幼兒園 收費", "幼兒園 超收"), "收費爭議"),
    "ntpc": (("新北 幼兒園", "板橋 幼兒園", "新莊 幼兒園"), "地區限定"),
}


@dataclasses.dataclass(frozen=True)
class PlanLine:
    channel: str
    label: str
    mode: str                    # "sweep"（廣掃）或 "per_institution"（逐園）
    target_ids: list[str]
    meter: dict
    blocker: str = ""            # 非空即不可執行，內容是給使用者看的原因

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["targets"] = len(self.target_ids)
        d.pop("target_ids")
        return d


@dataclasses.dataclass(frozen=True)
class ScanPlan:
    scope: str
    scope_label: str
    keywords: list[str]
    lines: list[PlanLine]
    #: 恆為全部園。縮小它會製造誤判，不是省成本。
    attribution_ids: list[str]
    usd_max: float
    unpriced: list[str]
    blockers: list[str]
    est_seconds: float

    def as_dict(self) -> dict:
        return {
            "scope": self.scope, "scope_label": self.scope_label,
            "keywords": self.keywords,
            "lines": [ln.as_dict() for ln in self.lines],
            "attribution_pool": len(self.attribution_ids),
            "usd_max": round(self.usd_max, 4), "unpriced": self.unpriced,
            "blockers": self.blockers, "est_seconds": round(self.est_seconds, 1),
        }


def resolve_scope(points: list[dict], scope: str, *, district: str = "",
                  top_n: int = 50, ids: list[str] | None = None,
                  proposal_ids: list[str] | None = None) -> tuple[list[dict], str]:
    """把範圍代號換成實際的機構清單與一句人看得懂的標籤。"""
    if scope == "district":
        sel = [p for p in points if p.get("d") == district]
        return sel, f"{district}（{len(sel)} 園）"
    if scope == "proposal":
        want = set(proposal_ids or [])
        sel = [p for p in points if p["i"] in want]
        return sel, f"本批派工提案（{len(sel)} 園）"
    if scope == "compliance_fail":
        sel = [p for p in points if p.get("cf", 0) > 0 or p.get("ch", 0) > 0]
        return sel, f"財報法遵未通過（{len(sel)} 園）"
    if scope == "evaluation":
        sel = [p for p in points if p.get("ep", 0) > 0]
        return sel, f"近兩年評鑑部分指標未通過（{len(sel)} 園）"
    if scope == "top_risk":
        sel = sorted(points, key=lambda p: p.get("r", 10**9))[:max(1, top_n)]
        return sel, f"分數前 {len(sel)} 名"
    if scope == "picked":
        want = set(ids or [])
        sel = [p for p in points if p["i"] in want]
        return sel, f"指定 {len(sel)} 園"
    return list(points), f"全市 {len(points)} 園"


def build_plan(points: list[dict], *, scope: str = "city",
               channels: list[str] | None = None,
               keywords: list[str] | None = None, max_posts: int = 50,
               district: str = "", top_n: int = 50,
               ids: list[str] | None = None,
               proposal_ids: list[str] | None = None,
               places_used_this_month: int = 0,
               apify_plan: str | None = None,
               has_place_id: set[str] | None = None) -> ScanPlan:
    """算出這次掃描的每一條線與總金額上界。不發請求、不寫檔。

    ``has_place_id`` 的鍵必須與 ``points`` 的 ``i`` 同一種——payload 用的是
    UUID 前 8 碼，``place_ids_ntpc.csv`` 存的是完整 UUID。傳錯會靜靜地把
    Google 評論那條線的目標算成 0，看起來像「這些園都沒有 place_id」。
    """
    # 空清單是「使用者把管道全部取消勾選」，不是「沒指定」。用 `or` 會把前者
    # 當成後者，於是執行兩條他剛剛明確關掉的線。
    channels = ["news_rss", "ptt"] if channels is None else channels
    # 與 channels 同一個陷阱：空清單是「使用者清空了關鍵字欄」，不是
    # 「沒指定」。`or` 會回退成預設值，讓人付錢買一個他沒有輸入的查詢。
    if keywords is None:
        keywords = list(DEFAULT_KEYWORDS)
    keywords = [k.strip() for k in keywords if k.strip()]
    targets, label = resolve_scope(points, scope, district=district, top_n=top_n,
                                   ids=ids, proposal_ids=proposal_ids)
    target_ids = [p["i"] for p in targets]
    lines: list[PlanLine] = []

    if "apify_threads" in channels:
        m = pricing.apify_meter(max_posts, plan=apify_plan, keywords=len(keywords))
        # Threads 恆為廣掃。逐園在此無法表達——不是預設關閉，是沒有這條路徑：
        # 50 家 × US$0.205 = US$10.25，一次耗盡整個月的額度還不夠。
        lines.append(PlanLine(
            channel="apify_threads", label=m.label, mode="sweep",
            target_ids=[], meter=m.as_dict()))

    if "places_reviews" in channels:
        pool = [i for i in target_ids
                if has_place_id is None or i in has_place_id]
        blocker = ""
        if target_ids and has_place_id is not None and not pool:
            # 全數落空通常是鍵對錯了，不是真的都沒有 place_id。說出來，
            # 不要讓一個空清單被讀成「查過了，沒有」。
            blocker = (f"{len(target_ids)} 家目標中無任何一家有 place_id。"
                       "請確認 has_place_id 的鍵與 points 的 i 一致"
                       "（payload 用 UUID 前 8 碼）。")
        if len(pool) > pricing.PER_INSTITUTION_CAP:
            blocker = (f"逐園查詢上限 {pricing.PER_INSTITUTION_CAP} 家，"
                       f"此範圍有 {len(pool)} 家。請改用較窄的範圍。")
        m = pricing.places_meter(len(pool) if not blocker else 0,
                                 used_this_month=places_used_this_month)
        lines.append(PlanLine(
            channel="places_reviews", label=m.label, mode="per_institution",
            target_ids=pool if not blocker else [], meter=m.as_dict(),
            blocker=blocker))

    for key, lbl in (("news_rss", "新聞（Google News RSS）"),
                     ("ptt", "PTT（親子與地區板）")):
        if key not in channels:
            continue
        # 免費管道：全市用廣掃（範圍只影響排序），窄範圍才逐園。
        sweep = scope == "city" or len(target_ids) > pricing.PER_INSTITUTION_CAP
        m = pricing.free_meter(key, lbl, len(target_ids), sweep=sweep)
        lines.append(PlanLine(
            channel=key, label=lbl,
            mode="sweep" if sweep else "per_institution",
            target_ids=[] if sweep else target_ids, meter=m.as_dict()))

    usd = sum(ln.meter["usd_max"] or 0.0 for ln in lines if not ln.blocker)
    blockers = [ln.blocker for ln in lines if ln.blocker]
    if "apify_threads" in channels and not keywords:
        # 沒有關鍵字時 sweep() 會回退成「幼兒園」——使用者會付錢買一個
        # 他沒有輸入的查詢。寧可擋下來。
        blockers.append("已選 Threads 但未輸入關鍵字。")
    if scope == "district" and not district:
        blockers.append("範圍選了行政區但未指定是哪一區。")
    if scope in ("picked", "proposal") and not targets:
        blockers.append(f"「{label}」沒有任何機構，無可掃描對象。")
    if not lines:
        # 沒有線的計畫金額是 0，但那不是「免費」——那是「什麼都不會發生」。
        # 不說清楚的話按鈕會顯示「開始掃描（不產生費用）」然後什麼也沒做。
        blockers.append("未選擇任何管道。請至少勾選一個。")
    return ScanPlan(
        scope=scope, scope_label=label, keywords=keywords, lines=lines,
        # 恆為全部園。這是歸屬正確性的前提，不是效能取捨。
        attribution_ids=[p["i"] for p in points],
        usd_max=round(usd, 4),
        unpriced=[ln.channel for ln in lines if ln.meter["usd_max"] is None],
        blockers=blockers,
        est_seconds=max((ln.meter["est_seconds"] for ln in lines), default=0.0),
    )
