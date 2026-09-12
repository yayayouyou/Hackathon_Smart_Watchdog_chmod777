"""社群聲音面板：把既有的三個來源並排開成一份前端可以照著刻的契約。

三個來源本來就各自存在，缺的是**把它們放在同一個畫面上**的那一層：

* Threads `@標註` 通報在資料庫裡（`realtime/mention_store.py`）
* 新聞與 PTT 在即時管道裡（`realtime/monitor.py` + `realtime/sources.py`）
* Google 評論在卷宗那支端點裡（`/api/reviews/{id}`，本檔的 `reviews_of()`）

定位與 `api/dossier.py` 相同：**把既有能力開成端點，不自己重讀一次資料、
不自己再算一次歸屬。** 歸屬一律是寫入時算好的（`mention_store`）或管道自己
算的（`alerts.attribute()`），這裡只負責排版。

四條界線，違反任何一條這支端點就變成了它不該是的東西：

1. **不回任何彙總分數、評分、星等平均、風險等級。** 系統輸出是「建議查核」的
   稽查優先序，不是「疑似不法」的認定（CLAUDE.md 輸出定位）；社群內容是未查證
   線索，`06-plan` §1 明訂不把社群聲量併入永久風險分數。所以回的是**可點回原文
   的清單**，判斷留給人。`counts` 裡也刻意沒有跨來源總計——見 `COUNTS_NOTE`。
2. **`mention` 與 `reply` 分開計數。** 一串十則「+1」繼承歸屬下來，合併計數會讓
   「有 11 個人向教育局反映」這句話灌水十倍。`06-plan` §6：分析單位是園所 ×
   事件群集，不是貼文數。跨機構列表同理——排序看的是時間，計數看的是**串數**。
3. **零聲量不等於低風險。** 每一段都帶 `has_signal` 與 `reason`，空陣列不准自己
   解讀成「沒事」。CLAUDE.md：不確定時標「資料不足」而非「低風險」。
4. **Google 評論 `may_store=False`，即時取用、即時回傳、不落地。**

**為什麼即時執行的管道只有免費那幾條。** 面板會被反覆重整，而 `apify_threads`
每次呼叫都是一次計費執行（`realtime/apify.py` 的說明：逐園查詢 50 家就是
US$10.25）。付費管道一律走 `/api/scan` 的三段授權流程；Google 評論是唯一的
例外，因為 `reviews_of()` 自己帶額度閘門（`realtime/ledger.py`）。這也是本檔
不直接呼叫 `monitor.watch()` 的預設管道清單、而是明確指定子集的理由——
預設清單在有金鑰時會把 `places_reviews` 算成 live，於是繞過那道閘門。
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import pathlib
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db.session import get_db
from ..realtime import classify, demo_data, mention_store, news_classify

router = APIRouter(prefix="/api/social", tags=["social"])

ROOT = pathlib.Path(__file__).resolve().parents[3]
INSTITUTIONS = ROOT / "data/processed/institutions_ntpc.csv"
PLACE_IDS = ROOT / "data/processed/place_ids_ntpc.csv"

#: 每一支端點都回。三支都可能被單獨呼叫，所以界線不能只寫在其中一支上。
DISCLAIMER = ("以下為未經查證的公開社群內容，僅供稽查人員研判參考；"
              "不是違法認定，也不計入風險分數。")

#: `counts` 旁邊的固定說明。前端最可能犯的錯就是把幾個數字加起來當成一個
#: 「聲量」，而那正是 06-plan §6 禁止的重複加權。
COUNTS_NOTE = ("各來源計數不可相加：一則 @標註、一則串下回覆、一篇新聞、"
               "一則 Google 評論不是同一種單位。要講「多少人向教育局反映」時"
               "看的是 threads.mentions（@標註我方的主貼文數），不是總列數。")

#: Google 評論的定位，量測結果決定的。放成常數是為了讓 `/api/reviews/{id}` 與
#: 本面板講的是**同一句話**——彙總時被換掉一次，這個定位就沒了。
REVIEWS_NOTE = ("家長主觀評價，非法遵指標。實測：裁罰 ≥5 件的園評分中位 4.20，"
                "無裁罰者 4.60（p=0.061 不顯著），個案不具鑑別力。")

#: 開啟面板時會**即時執行**的文字管道。理由見模組說明最後一段。
LIVE_MENTION_CHANNELS = ("news_rss", "ptt", "vendor_feed")

#: 管道 key → 它餵給回應的哪一段。`sources` 有七條、畫面只有三塊，前端靠這個
#: 對回去；`None` 代表這條管道目前不餵任何一段（待核准或需另行授權執行）。
FEEDS = {
    "news_rss": "mentions",
    "ptt": "mentions",
    "vendor_feed": "mentions",
    # 只會出現在建置時那次全市掃描的快照裡，面板不即時執行（計費）。
    "apify_threads": "mentions",
    "threads_mentions": "threads",
    # 關鍵字搜尋待 App Review；核准後併入 mentions。
    "threads": None,
    "places_reviews": "reviews",
}

#: 開啟面板時這條管道會不會真的被呼叫。與 `available` 是兩件事：
#: `threads_mentions` 可能是 live 的，但面板讀的是資料庫既有通報，
#: 不會替你去打一次 API（同步由 `scripts/sync_threads_mentions.py` 負責）。
RUNS_ON_OPEN = {"news_rss", "ptt", "vendor_feed", "places_reviews"}

#: 沒開通時「為什麼」的一句話。缺的是授權不是資料——這個分別要講得出來，
#: 理由見 `realtime/sources.py` 的模組說明。
_WHY_NOT = {
    "needs_api_key": "缺金鑰：尚未提供憑證，本管道目前沒有在看",
    "needs_approval": "缺授權：平台尚未核准申請，本管道目前沒有在看",
    "needs_procurement": "缺採購：需向資料服務供應商訂閱，本管道目前沒有在看",
}

#: 「沒有訊號」與「沒有異常」的分界線，逐字固定下來。
_QUIET = "無訊號不等於無異常；本系統僅代表未取得公開社群內容。"

#: 語氣分類的層級界線，逐字固定下來，三支端點共用。**上色在貼文，不在機構。**
#: 把一整園染成一個顏色，等於用未查證的貼文對一家真實機構下風險判斷——
#: `features/alerts.py` 整支模組就是為了不製造這種傷害而存在的（它的模組說明
#: 記了四個真實誤配，其中一則是某園公開澄清自己被誤認為新聞裡的那一家）。
#: 所以機構那一層回的是**組成**（三則負面、一則中性、一則詢問），不是一個值。
TONE_NOTE = ("語氣分類是對**單一貼文**的標記，不是對機構的評價，也不進任何分數"
             "（06-plan §1）。機構層只回各語氣的則數組成，不回單一語氣、"
             "不回風險等級；未分類代表尚未跑過分類，不是中性、也不是沒有問題。")

#: 新聞／PTT 的標籤定位，逐字沿用 `realtime/news_classify.py` 的那一句。
#: **兩套標籤不共用、計數不合併**，理由見那支模組的模組說明：一則民眾抱怨
#: （未查證的陳述）與一件已經起訴的案子（已發生的官方行動）不是同一種東西，
#: 共用同一個「語氣負面」標籤就是把稽查員判斷輕重的依據抹掉。
LABEL_NOTE = news_classify.LABEL_NOTE

#: 示範機構的定位，逐字固定下來。`is_demo` 為 true 的每一列都適用。
#: 示範內容不准指名真實機構——編造的投訴掛在真機構名下就是捏造指控，
#: 與 CLAUDE.md「輸出定位」同一條界線。理由見 `realtime/demo_data.py`。
DEMO_NOTE = ("is_demo 為 true 的機構是示範用的虛構園所（data/demo/"
             "institutions_demo.csv），不存在於真實主檔、不進風險分數、"
             "不進 payload。畫面上必須標示，不可與真實機構混為一談。")

#: 跨機構列表掃多少列資料庫。列表要先分組再排序，所以不能靠 SQL 的 LIMIT——
#: 那會在分組之前就把某一園的較舊那幾列切掉，counts 就少了。
_ROW_SCAN = 2000

#: 單園面板最多讀幾列該園的通報。一串二十則的討論也在這個數量級之內。
_THREAD_ROWS = 500


# ── 共用查表 ──────────────────────────────────────────────────────────


def _payload() -> dict:
    from . import server

    return server.payload()


def _index() -> dict[str, dict]:
    """8 碼 id → payload 上的那一點。"""
    return {p["i"]: p for p in _payload().get("points", [])}


def _snapshot() -> dict:
    """建置時那次全市掃描的結果（payload 的 `realtime` 區塊）。

    跨機構列表讀這個而不是即時掃 1,213 園：一次列表請求打上千次外部查詢，
    既慢又會被限流，而那次掃描的結果本來就在 payload 裡。**回應會標明每一則
    是 live 還是 snapshot**，因為「今天查到的」與「上次掃描查到的」是兩件事。
    """
    return _payload().get("realtime") or {}


@functools.lru_cache(maxsize=1)
def _master() -> dict[str, dict]:
    """8 碼 id → 完整 UUID 與園名。

    payload 的 `points[].i` 是 UUID 的前 8 碼，`threads_mention.institution_id`
    存的是完整 UUID——兩邊要對得起來就必須有這張表。缺檔時回空的，面板照樣開得
    起來：Threads 那一段會查不到，而那會被如實說成查不到，不會變成「沒有人通報」。
    """
    if not INSTITUTIONS.exists():
        return {}
    out: dict[str, dict] = {}
    with INSTITUTIONS.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            full = str(row.get("id") or "")
            if full:
                out.setdefault(full[:8], {
                    "id": full,
                    "title": row.get("title", ""),
                    "town": row.get("town", ""),
                })
    # 示範機構（`realtime/demo_data.py`）也要查得到，否則指名它們的示範貼文
    # 在畫面上會變成「認不出是哪一園」。它們只到得了這張查表——payload、
    # 風險分數、`data/processed/` 的任何一個檔案都沒有它們的路徑。
    for inst in demo_data.institutions():
        full = str(inst["id"])
        out.setdefault(full[:8], {"id": full, "title": inst["title"],
                                  "town": inst["town"]})
    return out


def _title_of(full_id: Optional[str]) -> str:
    if not full_id:
        return ""
    return (_master().get(str(full_id)[:8]) or {}).get("title", "")


def _institution(institution_id: str) -> dict:
    """接受 8 碼 id 或完整 UUID，兩種都認得。

    前端手上拿到的是哪一種要看它從哪一支端點來的：地圖與卷宗給 8 碼，
    Threads 通報給完整 UUID。兩邊都能直接丟進來，省掉前端自己截字串——
    截錯一碼的結果是 404，而那看起來會像資料不見了。
    """
    short = str(institution_id or "")[:8]
    point = _index().get(short) or {}
    master = _master().get(short) or {}
    if not point and not master:
        raise HTTPException(404, f"查無機構 {institution_id}")
    given = str(institution_id or "")
    return {
        "id": short,
        "full_id": master.get("id") or (given if len(given) > 8 else short),
        "title": point.get("full") or master.get("title", ""),
        "town": point.get("d") or master.get("town", ""),
        # 示範機構要自己說出來。名字看得出是假的不算數：畫面會被截圖，
        # 而截圖裡沒有人可以問「這一家是真的嗎」。判斷靠 id 不靠名字。
        "is_demo": demo_data.is_demo(short),
    }


def _sources() -> list[dict]:
    """每個管道的現況。**沿用 `Channel.describe()`**，不另寫一份。

    管道沒開要說「缺授權」而不是靜靜回空陣列：一份安靜的空清單會教操作的人
    這一園很平靜，而事實是沒有人在聽。理由見 `realtime/sources.py` 的模組說明。
    """
    from ..realtime.sources import LIVE, default_channels

    out = []
    for ch in default_channels():
        described = ch.describe()
        live = ch.status == LIVE
        out.append({
            **described,
            "available": live,
            "reason": "" if live else _WHY_NOT.get(ch.status, "尚未開通"),
            "feeds": FEEDS.get(described["key"]),
            "runs_on_open": described["key"] in RUNS_ON_OPEN,
        })
    return out


def _source_map() -> dict[str, dict]:
    return {s["key"]: s for s in _sources()}


def _date10(value: Any) -> str:
    return str(value or "")[:10]


def _zero_mention_counts() -> dict[str, int]:
    """每一條餵 `mentions` 的管道都先擺一個 0。

    只在有結果時才出現該 key 的話，前端讀到的是 `undefined`——那會被寫成
    「這個管道還沒查」或直接 NaN，而正確的意思是「查了，沒有」。零要看得見。
    """
    return {key: 0 for key, feeds in FEEDS.items() if feeds == "mentions"}


# ── Threads @標註 ─────────────────────────────────────────────────────


def _post(row: dict, full_id: str) -> dict:
    """一則通報，前端要能直接畫出來並點回原文。"""
    return {
        "threads_id": row["threads_id"],
        "kind": row["kind"],
        "username": row["username"],
        "text": row["text"],
        "permalink": row["permalink"],
        "posted_at": row["posted_at"],
        "observed_at": row["observed_at"],
        "attribution_source": row["attribution_source"],
        "attribution_basis": row["attribution_basis"],
        "institution_id": row["institution_id"],
        "institution_title": _title_of(row["institution_id"]),
        # 這一則指名的是示範機構嗎。前端照這個印「示範資料」，不自己看名字。
        "is_demo": demo_data.is_demo(row["institution_id"]),
        "reply_to_threads_id": row["reply_to_threads_id"],
        "is_reply": row["is_reply"],
        # 這一串裡可能混著別家的回覆（「吉尼爾也這樣」）。縮排會讓人讀成
        # 「這則也是在講上面那一園」，所以每一則都自己說是不是。
        "is_this_institution": row["institution_id"] == full_id,
        # ── 分類（`realtime/classify.py`）──────────────────────────
        # 全是封閉值，沒有摘要、沒有引文、沒有建議、沒有分數。模型寫的自由
        # 文字（`issues`）刻意不經過這裡：它的內容受外部貼文影響，印在官方
        # 主控台上等於讓陌生人的句子借用系統的口吻說話。要看原文點 permalink。
        "event_category": row["event_category"],
        "tone": row["tone"],
        "specificity": row["specificity"],
        "stance": row["stance"],
        "contains_minor_identifiers": row["contains_minor_identifiers"],
        "classified_at": row["classified_at"],
        # 前端該把這一則畫成哪一堆。**`classified_at` 為空時是 `unclassified`，
        # 不是 `neutral`**——前端若自己寫 `tone || "neutral"`，132 則沒有人看過
        # 的貼文會一次變成中性。所以這個判斷在後端做一次，前端照著畫。
        "tone_bucket": classify.tone_of(row),
        "tone_label": classify.TONE_LABELS[classify.tone_of(row)],
    }


def threads_of(db: Session, full_id: str, *, limit: int = _THREAD_ROWS) -> dict:
    """該園的 @標註串，主貼文 + 它底下的回覆。

    串是用 `mention_store.thread()` 取的，所以**整串都會回來**，包含那些自身
    指名了別家的回覆——串是討論的容器，不是主體的容器，把它們濾掉會讓複查的人
    看到一段沒有上下文的對話。計數則只算歸屬到這一園的列，否則別家那則會被
    數進這一園的「有幾個人反映」。
    """
    rows = mention_store.recent(db, limit=limit, institution_id=full_id)
    roots: list[str] = []
    for row in rows:
        # 早期的主貼文列 `root_threads_id` 是 NULL（那時還沒有這個欄位），
        # 所以它自己就是 root。`thread()` 兩種都查得到。
        root = row["root_threads_id"] or row["threads_id"]
        if root not in roots:
            roots.append(root)

    items = []
    for root in roots:
        posts = [_post(r, full_id) for r in mention_store.thread(db, root)]
        if not posts:
            continue
        head = posts[0] if posts[0]["threads_id"] == root else None
        others: dict[str, dict] = {}
        for p in posts:
            if p["is_this_institution"]:
                continue
            key = str(p["institution_id"] or "")
            entry = others.setdefault(key, {
                "institution_id": p["institution_id"],
                "title": p["institution_title"],
                "is_demo": p["is_demo"],
                "posts": 0,
            })
            entry["posts"] += 1
        mine = [p for p in posts if p["is_this_institution"]]
        items.append({
            "root_threads_id": root,
            "root": head,
            # 只抓得到回覆、主貼文不在庫裡是可能的；那時 root 是 None 而不是
            # 拿第一則回覆頂替——頂替會讓畫面上出現一個不存在的「原PO」。
            "root_missing_reason": "" if head else "主貼文不在庫裡，只同步到串下回覆",
            "replies": [p for p in posts if p["threads_id"] != root],
            "permalink": (head or posts[0])["permalink"],
            "started_at": (head or posts[0])["posted_at"],
            "last_activity": max(p["posted_at"] or "" for p in posts),
            "counts": {
                "mentions": sum(1 for p in mine if p["kind"] == mention_store.MENTION),
                "replies": sum(1 for p in mine if p["kind"] == mention_store.REPLY),
                "posts_in_thread": len(posts),
            },
            # 語氣**組成**，不是單一語氣，所以它不住在 `counts` 裡——`counts`
            # 數的是貼文，這個講的是那些貼文長什麼樣。只算歸屬到這一園的那幾則：
            # 串裡指名別家的那則仍會顯示，但把它計進來就是替這一園多記一筆
            # 別人的抱怨。
            "tone": classify.compose(mine),
            # 這一串裡歸屬到別家的貼文。空的是常態；有值代表一串討論裡冒出了
            # 第二家，那是新線索不是雜訊。
            "other_institutions": list(others.values()),
        })

    src = _source_map().get("threads_mentions", {})
    available = bool(src.get("available"))
    counts = {
        "threads": len(items),
        "mentions": sum(1 for r in rows if r["kind"] == mention_store.MENTION),
        "replies": sum(1 for r in rows if r["kind"] == mention_store.REPLY),
    }
    if items:
        reason = ""
    elif not available:
        reason = (f"Threads @標註管道尚未開啟（{src.get('reason', '尚未開通')}）；"
                  f"目前沒有在收，{_QUIET}")
    else:
        reason = f"此管道目前無訊號：沒有指名這一園的 @標註通報。{_QUIET}"
    return {
        "available": available,
        "has_signal": bool(items),
        "reason": reason,
        "counts": counts,
        # 這一園的語氣組成。**沒有「主要語氣」欄位，也不會有**——理由見 TONE_NOTE。
        "tone": classify.compose(rows),
        "tone_note": TONE_NOTE,
        "items": items,
    }


# ── 新聞／PTT 即時 ────────────────────────────────────────────────────


def _live_item(mention: dict, labels: dict) -> dict:
    # `decorate()` 只查 sidecar（`data/runtime/news_labels.jsonl`），不打模型：
    # 面板會被反覆重整，而分類是要錢的。補跑走
    # `scripts/classify_news_mentions.py`，查不到就照實回「未分類」。
    return news_classify.decorate({
        "channel": mention.get("channel", ""),
        "channel_label": labels.get(mention.get("channel", ""), mention.get("channel", "")),
        "headline": mention.get("headline", ""),
        "url": mention.get("url", ""),
        "published": mention.get("published", ""),
        "publisher": mention.get("publisher", ""),
        "attribution_basis": mention.get("attribution_basis", ""),
        # 即時結果不做關鍵字分類；null 是「沒有這個欄位」，不是「分類為無」。
        # 模型那一層（`report_kind`）不看這個欄位，兩層各自獨立。
        "kind": None,
        "source": "live",
        "may_store": bool(mention.get("stored", False)),
    })


def _snapshot_item(row: dict, labels: dict) -> dict:
    return news_classify.decorate({
        "channel": row.get("ch", ""),
        "channel_label": labels.get(row.get("ch", ""), row.get("ch", "")),
        "headline": row.get("h", ""),
        "url": row.get("u", ""),
        "published": row.get("d", ""),
        "publisher": row.get("p", ""),
        # 快照沒有保留歸屬依據。null 代表「當時沒記下來」，不是「沒有依據」。
        "attribution_basis": None,
        # 關鍵字表（`features/alerts.py::article_kind()`）當時記下的那個值。
        # **原樣留著，不被模型結果覆蓋**：兩者不一致的那幾則正是最該看的。
        "kind": row.get("k"),
        "source": "snapshot",
        "may_store": True,
    })


def mentions_of(institution: dict, *, live: bool = True, limit: int = 20) -> dict:
    """新聞與 PTT。即時查一次，並補上建置時那次全市掃描留下的結果。

    兩種來源在 `source` 欄位上分得開（`live`／`snapshot`）：「今天查到的」與
    「上次掃描查到的」是兩件事，混在一起會讓一則四個月前的新聞看起來像今天的。
    """
    from ..realtime.monitor import watch
    from ..realtime.sources import LIVE, default_channels

    labels = {k: v["label"] for k, v in _source_map().items()}
    snapshot = _snapshot()
    snap_items = [_snapshot_item(r, labels) for r in
                  (snapshot.get("by_institution") or {}).get(institution["id"], [])]

    channels = [c for c in default_channels() if c.key in LIVE_MENTION_CHANNELS]
    ran: list[str] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    live_items: list[dict] = []

    # 示範機構不去查外面。拿一個虛構園名打新聞與 PTT，回來的只會是空的；
    # 萬一不是空的，那就是把一篇關於**真實**機構的報導掛到了假園名下。
    if live and demo_data.is_demo(institution["id"]):
        live = False
        skipped = [{"key": c.key, "reason": "示範機構，不對外查詢"}
                   for c in channels]
    if live:
        result = watch({"id": institution["id"], "title": institution["title"],
                        "town": institution["town"]},
                       channels=channels, limit=limit)
        live_items = [_live_item(m, labels) for m in result.get("mentions", [])]
        errors = result.get("errors", [])
        ran = [c.key for c in channels if c.status == LIVE]
        skipped = [{"key": c.key,
                    "reason": _WHY_NOT.get(c.status, "尚未開通")}
                   for c in channels if c.status != LIVE]
    elif not skipped:
        skipped = [{"key": c.key, "reason": "本次以 live=false 呼叫，未即時查詢"}
                   for c in channels]

    seen = {i["url"] for i in live_items if i["url"]}
    items = live_items + [i for i in snap_items if i["url"] not in seen]
    items.sort(key=lambda i: _date10(i["published"]), reverse=True)

    counts = _zero_mention_counts()
    for item in items:
        counts[item["channel"]] = counts.get(item["channel"], 0) + 1
    counts["total"] = len(items)

    if items:
        reason = ""
    elif not ran:
        reason = (f"新聞與 PTT 本次未執行（{'；'.join(s['reason'] for s in skipped)}）；"
                  f"{_QUIET}")
    else:
        reason = f"此管道目前無訊號：查無指名這一園的公開新聞或討論。{_QUIET}"

    return {
        "available": bool(ran),
        "has_signal": bool(items),
        "reason": reason,
        "ran": ran,
        "skipped": skipped,
        "errors": errors,
        "snapshot_swept_at": snapshot.get("swept_at", ""),
        "counts": counts,
        # 報導**性質**的組成，不是語氣組成，所以它住在自己的欄位裡而不是
        # 併進 `threads.tone`。兩邊的分母是兩種東西：那邊數的是未查證的民眾
        # 陳述，這邊數的是已經見報的報導。加起來就分不出來了。
        "labels": news_classify.compose(items),
        "label_note": LABEL_NOTE,
        "items": items,
    }


# ── Google 評論 ───────────────────────────────────────────────────────


def reviews_of(institution_id: str) -> dict:
    """Google 評論，開卷宗時即時取用。

    量測結果決定了它的定位：裁罰 ≥5 件的園評分中位 4.20、無裁罰者 4.60
    （p=0.061，不顯著），而個案完全不具鑑別力——16 件裁罰的幼苗國際 4.7 星、
    13 件的南蒂亞 4.9 星、因虐童停招的吉尼爾 4.4 星。**不是風險訊號**，
    是稽查員到場前值得看一眼的家長觀感。

    `may_store=False`：即時取用、即時回傳，**不入庫、不進特徵、不影響排序**
    （Places API ToS 3.2.3(a)(b)）。搬到這裡是為了讓 `/api/reviews/{id}` 與
    社群面板呼叫的是同一支函式——各寫一份的那天，就是兩個畫面對同一家園講出
    不同星等的那天。
    """
    from .. import config

    payload = _payload()
    key = config.get("GOOGLE_MAPS_API_KEY")
    p = {pt["i"]: pt for pt in payload.get("points", [])}.get(institution_id)
    if not p:
        raise HTTPException(404, f"查無機構 {institution_id}")
    if not key:
        return {"available": False, "reason": "未設定 GOOGLE_MAPS_API_KEY",
                "reviews": []}

    place_id = ""
    if PLACE_IDS.exists():
        with PLACE_IDS.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["id"][:8] == institution_id or row["id"] == institution_id:
                    place_id = row.get("place_id", "")
                    break
    if not place_id:
        return {"available": False,
                "reason": "尚未解析 place_id（執行 scripts/resolve_place_ids.py）",
                "reviews": []}

    import json as _json
    import urllib.error
    import urllib.request

    # 免費額度是全域的，計費器就必須是全域的——否則「本月 138/1,000」在上線
    # 第一天就是錯的，而 1,000 次會在某個沒人按過「掃描」的下午被開卷宗耗盡。
    from ..realtime import ledger as _ledger

    # 記帳不等於管制。開卷宗這條路徑先前只記數不檢查，免費額度用罄後
    # 每開一次就是一次計費請求，而且完全不受任何上限約束。
    from ..realtime import pricing as _pricing

    unit = _pricing.PLACES_WITH_REVIEWS[1]
    gate = _ledger.check(unit)
    if not gate["ok"]:
        return {"available": False,
                "reason": f"查詢額度已用盡：{gate['reason']}",
                "reviews": [],
                "note": "這是額度限制，不是「這家園沒有評論」。"}
    _ledger.note_call("places_reviews", 1, detail=f"dossier:{institution_id}")
    _rid = _ledger.reserve(unit, "places_reviews", "dossier",
                           detail=institution_id)

    req = urllib.request.Request(
        f"https://places.googleapis.com/v1/places/{place_id}",
        headers={"X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": "id,displayName,rating,userRatingCount,"
                                     "googleMapsUri,reviews"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = _json.loads(r.read(500_000))
    except urllib.error.HTTPError as e:
        # 請求送出了就是計費了，即使回錯誤——所以結算成實付而不是釋放。
        _ledger.settle(_rid, unit, provider_ref=f"dossier:{institution_id}")
        return {"available": False, "reason": f"HTTP {e.code}", "reviews": []}
    _ledger.settle(_rid, unit, provider_ref=f"dossier:{institution_id}")

    return {
        "available": True,
        "name": (data.get("displayName") or {}).get("text", ""),
        "rating": data.get("rating"),
        "review_count": data.get("userRatingCount"),
        "maps_uri": data.get("googleMapsUri", ""),
        "reviews": [{
            "text": (rv.get("originalText") or rv.get("text") or {}).get("text", ""),
            "author": (rv.get("authorAttribution") or {}).get("displayName", ""),
            "author_uri": (rv.get("authorAttribution") or {}).get("uri", ""),
            "rating": rv.get("rating"),
            "published": str(rv.get("publishTime", ""))[:10],
        } for rv in (data.get("reviews") or [])],
        "note": REVIEWS_NOTE,
        "free_remaining": max(
            0, 1000 - _ledger.budget().places_used_this_month),
        "meter_source": "本機計數，非 Google 帳單",
    }


def _reviews_block(institution_id: str) -> dict:
    """面板用的評論區塊：原樣沿用 `reviews_of()`，只補上 `has_signal`／`reason`。

    `note` 一定在，即使取不到評論——那句話是這個來源的定位，不是成功路徑的裝飾。
    量測結果被彙總洗掉的那一刻，星等就會被當成風險指標讀。

    `reason` 在這裡會被補上那句「無訊號不等於無異常」，讓三個區塊有同一條保證：
    **`has_signal` 為 false 時 `reason` 必定非空、且必定沒有暗示「沒事」。**
    前端少一個判斷分支，就少一個把缺金鑰畫成綠燈的機會。`/api/reviews/{id}`
    仍然回原本那句（它的呼叫端是卷宗，不是這個面板）。
    """
    if demo_data.is_demo(institution_id):
        # 示範機構沒有 Google 商家，也不在 payload 上。`reviews_of()` 對不在
        # payload 上的 id 是丟 404，而那個 404 會讓**整個面板**消失——看起來
        # 就像這一園不存在，而不是「這一段不適用」。
        return {"available": False, "has_signal": False,
                "reason": f"示範機構沒有 Google 商家資料，本段不適用。{_QUIET}",
                "reviews": [], "note": REVIEWS_NOTE, "may_store": False}
    try:
        raw = reviews_of(institution_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 一條管道壞掉不該讓整個面板 500
        return {"available": False, "has_signal": False,
                "reason": f"取用失敗：{type(exc).__name__}: {exc}；{_QUIET}",
                "reviews": [], "note": REVIEWS_NOTE, "may_store": False}
    reason = raw.get("reason", "")
    if raw.get("available") and not raw.get("reviews"):
        reason = f"此管道目前無訊號：Google 上沒有可顯示的評論。{_QUIET}"
    elif reason and _QUIET not in reason:
        # 缺金鑰、缺 place_id、額度用盡——沒有一項的意思是「這一園沒有評論」。
        reason = f"{reason}；{_QUIET}"
    return {
        **raw,
        "has_signal": bool(raw.get("reviews")),
        "reason": reason,
        "note": raw.get("note") or REVIEWS_NOTE,
        "may_store": False,
    }


# ── 端點 ─────────────────────────────────────────────────────────────
# ⚠️ `/unattributed` 必須宣告在 `/{institution_id}` **前面**，否則
# FastAPI 會把 "unattributed" 當成一個機構 id 去查，結果是 404。


@router.get("/unattributed")
def unattributed(limit: int = Query(50, ge=1, le=500),
                 db: Session = Depends(get_db)) -> dict:
    """歸屬拒配的通報佇列：`institution_id IS NULL` 的那些。

    **這是待人工認園的工作量，不是雜訊桶。** `features.alerts.attribute()` 的
    預設是拒絕（它的模組說明記了四個真實誤配案例），所以認不出來是常態而不是
    故障。把這些丟掉，畫面上就會變成從來沒有人通報過——與把 `$ -` 記成 0
    是同一種錯。

    每一筆帶原文全文與 permalink，因為認園這件事只能由讀過原文的人做。
    """
    rows = mention_store.recent(db, limit=limit, unattributed=True)
    src = _source_map().get("threads_mentions", {})
    items = [{
        "threads_id": r["threads_id"],
        "username": r["username"],
        "text": r["text"],
        "permalink": r["permalink"],
        "posted_at": r["posted_at"],
        "observed_at": r["observed_at"],
        "kind": r["kind"],
        "attribution_source": r["attribution_source"],
        # 為什麼拒配。認園的人要先知道機器是卡在哪裡。
        "attribution_basis": r["attribution_basis"],
        "root_threads_id": r["root_threads_id"],
        "reply_to_threads_id": r["reply_to_threads_id"],
    } for r in rows]
    return {
        "count": len(items),
        "limit": limit,
        "available": bool(src.get("available")),
        "has_signal": bool(items),
        "reason": "" if items else (
            f"目前沒有待認園的通報。{_QUIET}" if src.get("available") else
            f"Threads @標註管道尚未開啟（{src.get('reason', '尚未開通')}）。"),
        "items": items,
        "note": ("institution_id 為 NULL 代表「認不出是哪一園」，"
                 "不是「與機構無關」。這份清單是稽查工作量，需人工逐則認園。"),
        "disclaimer": DISCLAIMER,
    }


@router.get("")
def browse(limit: int = Query(50, ge=1, le=500),
           town: Optional[str] = None,
           channel: Optional[str] = None,
           since: Optional[str] = None,
           db: Session = Depends(get_db)) -> dict:
    """最近有社群聲音的機構，依 `last_activity` 新到舊。

    **這不是聲量排行榜。** 排序依據是時間，不是任何分數；回應裡也沒有分數欄位
    （`06-plan` §1：不把社群聲量併入永久風險分數）。要顯示數量時看的是**串數**
    `counts.threads.threads`，不是貼文數——一串十則「+1」是一件事，不是十件。

    跨機構的新聞／PTT 計數讀的是建置時那次全市掃描的快照，不即時重掃：一次
    列表請求打上千次外部查詢既慢又會被限流。單園面板才會即時查。
    """
    if since:
        try:
            dt.date.fromisoformat(_date10(since))
        except ValueError:
            raise HTTPException(400, "since 需為 ISO 日期（YYYY-MM-DD）") from None

    valid = set(_source_map()) | {"threads_mentions"}
    if channel and channel not in valid:
        # 靜靜回空清單會讓打錯字看起來像「這個管道什麼都沒有」。
        raise HTTPException(400, f"未知的管道 {channel}；可用：{sorted(valid)}")

    index = _index()
    agg: dict[str, dict] = {}

    def _entry(short: str) -> dict:
        return agg.setdefault(short, {
            "roots": set(), "mentions": 0, "replies": 0, "posts": [],
            # `posts` 餵語氣組成、`news` 餵報導性質組成。**兩個清單刻意分開**，
            # 因為它們永遠不會被加在一起——合在一個清單裡的那天就會有人這麼做。
            "channels": {}, "news": [], "latest": None, "last": "",
        })

    def _offer(entry: dict, stamp: str, latest: dict) -> None:
        """最新一則永遠是時間最大的那一則，不管它來自哪個來源。"""
        if stamp > entry["last"]:
            entry["last"], entry["latest"] = stamp, latest

    for row in mention_store.recent(db, limit=_ROW_SCAN):
        if not row["institution_id"]:
            continue                      # 拒配的在 /api/social/unattributed
        entry = _entry(str(row["institution_id"])[:8])
        entry["roots"].add(row["root_threads_id"] or row["threads_id"])
        entry["posts"].append(row)
        if row["kind"] == mention_store.REPLY:
            entry["replies"] += 1
        else:
            entry["mentions"] += 1
        _offer(entry, str(row["posted_at"] or ""), {
            "source": "threads_mentions",
            "kind": row["kind"],
            "summary": mention_store._headline(row["text"])[:120],
            "permalink": row["permalink"],
            "posted_at": row["posted_at"],
            "attribution_source": row["attribution_source"],
        })

    snapshot = _snapshot()
    for short, rows in (snapshot.get("by_institution") or {}).items():
        if not rows:
            continue
        entry = _entry(short)
        for row in rows:
            ch = row.get("ch", "")
            entry["channels"][ch] = entry["channels"].get(ch, 0) + 1
            entry["news"].append(news_classify.label_of(
                ch, row.get("u", ""), row.get("h", "")))
            _offer(entry, _date10(row.get("d")), {
                "source": ch,
                "kind": row.get("k"),
                "summary": str(row.get("h", ""))[:120],
                "permalink": row.get("u", ""),
                "posted_at": row.get("d", ""),
                "attribution_source": None,
            })

    items = []
    for short, entry in agg.items():
        point = index.get(short) or {}
        master = _master().get(short) or {}
        row_town = point.get("d") or master.get("town", "")
        if town and row_town != town:
            continue
        threads_n = len(entry["roots"])
        if channel == "threads_mentions" and not threads_n:
            continue
        if channel and channel != "threads_mentions" and not entry["channels"].get(channel):
            continue
        last_date = _date10(entry["last"])
        if since and last_date < _date10(since):
            continue
        mention_counts = {**_zero_mention_counts(), **entry["channels"]}
        mention_counts["total"] = sum(entry["channels"].values())
        items.append({
            "institution_id": short,
            "full_id": master.get("id") or short,
            "title": point.get("full") or master.get("title", ""),
            "town": row_town,
            # 示範機構（`realtime/demo_data.py`）。這一列在畫面上要標出來。
            "is_demo": demo_data.is_demo(short),
            # 來源給什麼就是什麼：Threads 給完整時戳，快照只給日期。
            "last_activity": entry["last"],
            "last_activity_date": last_date,
            "counts": {
                "threads": {"threads": threads_n,
                            "mentions": entry["mentions"],
                            "replies": entry["replies"]},
                "mentions": mention_counts,
            },
            # 這一列該印「3 則語氣負面 · 1 則中性 · 1 則詢問」，不是一個顏色。
            # 放在 `counts` 之外，因為 `counts` 的每一項都是「幾則」，而這一項
            # 是那幾則的組成——混進去會讓「各來源計數不可相加」那句話多一個
            # 不是計數的成員。理由見 TONE_NOTE。
            "tone": classify.compose(entry["posts"]),
            # 新聞／PTT 的性質組成。**與 `tone` 是兩個欄位，不相加、不合併成
            # 一個「關注度」**：那邊是未查證的民眾陳述，這邊是已經見報的報導，
            # 數成同一類就等於把「有人抱怨」與「已經起訴」畫上等號。
            "news": news_classify.compose(entry["news"]),
            "latest": entry["latest"],
            "has_signal": True,
        })

    # 時間新到舊。同一天的用串數當 tie-break，仍然不是排行榜——只是讓
    # 同日的那幾筆有個穩定順序。
    items.sort(key=lambda r: (r["last_activity_date"], r["last_activity"],
                              r["counts"]["threads"]["threads"]), reverse=True)
    shown = items[:limit]
    return {
        "count": len(shown),
        "matched": len(items),
        "limit": limit,
        "filters": {"town": town, "channel": channel, "since": since},
        "sources": _sources(),
        "snapshot_swept_at": snapshot.get("swept_at", ""),
        "coverage": {
            "listed": len(items),
            "institutions_total": len(index),
            "note": ("只列出目前有社群訊號的機構。未列出的機構代表本系統未取得"
                     "公開社群內容，不代表無異常。"),
        },
        "items": shown,
        "note": ("依最近活動時間排序，不是聲量排行榜；本端點不回任何分數。"
                 + COUNTS_NOTE),
        "tone_note": TONE_NOTE,
        "label_note": LABEL_NOTE,
        "demo_note": DEMO_NOTE,
        "disclaimer": DISCLAIMER,
    }


@router.get("/{institution_id}")
def institution_social(institution_id: str, live: bool = True,
                       db: Session = Depends(get_db)) -> dict:
    """單一機構的社群聲音全貌：管道現況、Threads 串、新聞／PTT、Google 評論。

    `live=false` 時不即時查新聞與 PTT，只回建置時快照——示範與測試用，
    也給只想看 Threads 那一段的呼叫端一條不等待外部請求的路。
    """
    inst = _institution(institution_id)
    threads = threads_of(db, inst["full_id"])
    mentions = mentions_of(inst, live=live)
    reviews = _reviews_block(inst["id"])
    has_signal = bool(threads["has_signal"] or mentions["has_signal"]
                      or reviews["has_signal"])
    return {
        "institution": inst,
        "sources": _sources(),
        "threads": threads,
        "mentions": mentions,
        "reviews": reviews,
        "counts": {
            "threads": threads["counts"],
            "mentions": mentions["counts"],
            "reviews": {"shown": len(reviews.get("reviews") or [])},
            "note": COUNTS_NOTE,
        },
        "has_signal": has_signal,
        "reason": "" if has_signal else (
            f"三個來源目前都沒有訊號。{_QUIET}"),
        # 文字住在後端，前端照印。示範與真實的分界線由一句話定義，
        # 而那句話有兩個呼叫端（列表與單園面板）——寫在前端就是寫兩次。
        "demo_note": DEMO_NOTE if inst["is_demo"] else "",
        "disclaimer": DISCLAIMER,
    }


# ── 擬定回覆 ──────────────────────────────────────────────────────────
# 路徑多一段（`/{id}/draft-reply`），所以與上面那支 catch-all GET 不會相撞；
# 方法也不同。`unattributed` 那個排序陷阱在這裡不成立。


class DraftReplyRequest(BaseModel):
    """`root_threads_id` 指定要回哪一串。`backend` 不給時自動選。

    `backend="template"` 是**離線與測試的那條路**，也是模型不通時的地板；
    `"bedrock"` 強制走生成。兩者的輸出都必須通過 `report/verify.py` 的閘門，
    所以選哪一個改變的是措辭，不是這份草稿被允許說什麼。
    """

    root_threads_id: str
    backend: Optional[str] = None


@router.post("/{institution_id}/draft-reply")
def draft_reply(institution_id: str, req: DraftReplyRequest,
                db: Session = Depends(get_db)) -> dict:
    """替一串 @標註通報擬一份回覆草稿。**只回文字，不送出任何東西。**

    這支端點是整個系統裡唯一一個以「民眾的貼文」為輸入產生對外文字的地方，
    所以它的三條界線寫在這裡而不只寫在 `report/reply.py`：

    1. **不發文。** 本函式不 import `scrape/threads.py`，那支模組也沒有任何
       發文、回覆或刪除的函式可以被 import。草稿走到承辦人畫面上就停住。
       官方帳號在任何人讀過內容之前自動回「已收到您的通報」，是一個公開的
       受理表態——`scrape/threads.py` 的模組說明把這件事講得很清楚。
    2. **標題取自那一串自己的歸屬，不取自網址。** 網址上的機構只是畫面脈絡；
       若那一串歸屬到別家，這裡回 400 而不是照著網址寫一個園名上去——
       草稿不得宣稱一個 `mention_store` 沒有做出來的歸屬。
    3. **草稿不入庫、不進分數、不進 payload。** 與 `threads_mention` 那張表的
       不變式一致：要進卷宗必須人工逐則採用，而且沒有那條程式路徑。
    """
    from ..report import reply as _reply

    inst = _institution(institution_id)
    root = str(req.root_threads_id or "").strip()
    if not root:
        raise HTTPException(400, "需要 root_threads_id")

    rows = mention_store.thread(db, root)
    if not rows:
        raise HTTPException(404, f"查無這一串通報 {root}")
    head = rows[0]
    if head["threads_id"] != root:
        # 只抓到回覆、主貼文不在庫裡。要回的那個人不在手上，草稿沒有收件人。
        raise HTTPException(
            409, f"主貼文 {root} 不在庫裡（只同步到串下回覆），無法擬定回覆")
    if head["institution_id"] and head["institution_id"] != inst["full_id"]:
        raise HTTPException(
            409, f"這一串歸屬到 {_title_of(head['institution_id']) or '另一家機構'}，"
                 f"不是 {inst['title']}；請從該園的面板擬定回覆")

    kind = (req.backend or "").strip().lower() or (
        "bedrock" if _reply.available() else "template")
    backend = _reply.get_backend(kind)
    # 標題取自那一串自己的歸屬。拒配的那些照樣可以擬稿，但草稿會說「未歸屬」
    # ——比在草稿上印一個猜出來的園名誠實。
    title = _title_of(head["institution_id"]) if head["institution_id"] else ""
    result = _reply.draft({"title": title}, rows, backend=backend)

    return {
        "institution": inst,
        "root_threads_id": root,
        "permalink": head["permalink"],
        "posts_used": len(rows),
        "attributed": bool(head["institution_id"]),
        "draft": result.text,
        # 可送出的那一段單獨給一次，讓前端不必自己切字串——切錯的後果是把
        # 「承辦人須知」連同園名一起貼到公開平台上。
        "sendable": result.sendable,
        "sendable_mark": _reply.SENDABLE_MARK,
        "backend": result.backend,
        "verified": result.verified,
        "problems": result.problems,
        "fell_back": result.fell_back,
        "fallback_reason": result.fallback_reason,
        "checklist": list(_reply.CHECKLIST),
        # 前端不得把這個值畫成「已送出」「已受理」或任何完成狀態。
        "auto_send": False,
        "note": ("草稿未送出，也不會自動送出：本系統對 Threads 只有讀取，"
                 "沒有任何發文路徑。送出與否、用什麼身分送，由承辦人決定。"),
        "disclaimer": DISCLAIMER,
    }
