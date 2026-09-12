"""HTTP服務：把 payload 契約端上來，前端從此用 fetch 取資料。

先前的靜態頁把整份 payload 烤進 HTML，因為 Artifact 的 CSP 擋掉 fetch、也擋掉
`maps.googleapis.com` 以外一切非白名單來源——地圖只能自己用 SVG 畫。真正的
web app 沒有這些限制，所以這一層一上來，Google 地圖、滾輪縮放、即時查詢就都
成立了。

``api/payload.py`` 從第一天就是為了這個切換而寫的：同一組函式，先前餵給
`build_frontend.py` 內嵌，現在餵給 HTTP。**payload 形狀完全不變**，因此舊的
靜態版與新的動態版讀的是同一份契約。

端點：

    GET  /api/payload            完整 payload（同 dist/data/payload.json）
    GET  /api/institutions       地圖用的精簡點位
    GET  /api/institutions/{id}  單園卷宗（含財報發現、員工、即時聲音）
    GET  /api/proposal?n=20      派工提案，與前端同一套分層規則
    GET  /api/realtime/{id}      即時重新掃描該園（不吃快取）
    GET  /api/social             最近有社群聲音的機構（依時間，非排行榜）
    GET  /api/social/{id}        單園社群聲音全貌（契約見 docs/api/social-panel.md）
    GET  /api/social/unattributed 歸屬拒配的通報佇列（待人工認園）
    POST /api/chat               自然語言查詢
    GET  /api/timeline           時間軸回測：每年重訓一次的實測成績
    GET  /api/timeline/{as_of}   某一格的逐園排序與事後命中
    GET  /api/docsearch?q=       文件索引：問題進、檔案與頁碼出
    GET  /api/health             各資料表與管道的就緒狀態

資料在啟動時載入一次進記憶體。全市 1,213 園的 payload 約 900 KB，重載成本
遠低於每次請求重算，而且讓 /api/health 能誠實回報「載入了什麼、少了什麼」。
"""

from __future__ import annotations

import contextlib
import json
import pathlib
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config

ROOT = pathlib.Path(__file__).resolve().parents[3]
WEBAPP = ROOT / "webapp"
PAYLOAD_PATH = ROOT / "dist/data/payload.json"
LAND_PATH = ROOT / "data/external/tw_neighbor_land.json"

# ── MCP ──────────────────────────────────────────────────────────────
# 同一組 tool 的第二條入口：瀏覽器走 /api/agent/messages，MCP 客戶端走 /mcp。
# 白名單與稽核都在 ToolRegistry.execute()，所以兩條路徑不可能有不同的權限。
#
# ⚠️ **必須在建立 app 之前先建好**，因為 FastMCP 的 StreamableHTTPSessionManager
# 要靠它自己的 lifespan 初始化 task group——只 mount 不接 lifespan 的話，
# mount 成功、tools/list 也列得出來，但**協定層的 initialize 會 500**
# （`Task group is not initialized`）。這個組合實測踩過。
#
# 掛載失敗不讓整個服務起不來：MCP 是額外通道，派工台本身不依賴它。
_mcp_app = None
try:
    from ..agent.mcp_server import build_mcp as _build_mcp

    _mcp_app = _build_mcp().http_app(path="/")
except Exception as _mcp_exc:  # noqa: BLE001 - 缺 fastmcp 或版本不符都只停用這條
    print(f"MCP 未掛載：{_mcp_exc}")


@contextlib.asynccontextmanager
async def _lifespan(app_: FastAPI):
    """啟動工作 + MCP 的 lifespan。

    改成 lifespan 而不是 `@app.on_event("startup")`，是因為 Starlette 一旦收到
    自訂 lifespan 就不再跑 on_event 的處理器——兩者不能並存。內容與原本那支
    `_startup` 完全相同，只是多包了 MCP 那一層。
    """
    # 建表。`create_all` 是冪等的，已存在就什麼都不做。
    #
    # 為什麼放在這裡而不是只靠 `scripts/seed_users.py`：那支是容器部署路徑
    # （見 docs/DEPLOY.md），但**本機 `run.py serve` 不會跑它**。結果是在一台
    # 剛 clone 的機器上，SQLAlchemy 連線時會把 sqlite 檔建出來卻沒有任何表，
    # 於是 `POST /api/auth/login` 回 **500 Internal Server Error**，
    # 訊息是 `no such table: user_session`——看起來像資料庫壞了，實際上是沒建過。
    # 這個情境已經真的發生過（合併後在本機實測）。
    try:
        from ..db.session import init_db

        init_db()
    except Exception as exc:  # noqa: BLE001 - 建表失敗不該讓整個服務起不來
        print(f"⚠️ 資料表初始化失敗（登入與助理會不可用）：{exc}")

    try:
        load_payload()
    except FileNotFoundError as exc:  # keep the server up so /api/health can say why
        print(f"⚠️ {exc}")
    _scan.bind(payload, get_proposal)
    # 重啟對帳：進行中的任務標為中斷，且**不釋放**已預留的額度。
    from ..realtime.jobs import STORE

    n = STORE.sweep_interrupted()
    if n:
        print(f"⚠️ {n} 個掃描任務因重啟中斷；預留額度維持佔用（當機的執行照樣花了錢）")

    if _mcp_app is None:
        yield
    else:
        async with _mcp_app.lifespan(app_):
            yield


app = FastAPI(
    title="小小守護員 Smart Watchdog",
    description="新北市教保機構稽查優先序。輸出為建議查核，非違法認定。",
    version="1.0",
    lifespan=_lifespan,
)
# The console may be served from a different origin during development.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_state: dict[str, Any] = {"payload": None, "index": {}, "land": None}

# 掃描主控台與證據端點。在此掛載而非讓子模組匯入 server，避免循環匯入。
from . import agent as _agent  # noqa: E402
from . import auth as _auth  # noqa: E402
from . import dataroom as _dataroom  # noqa: E402
from . import dossier as _dossier  # noqa: E402
from . import evidence as _evidence  # noqa: E402
from . import explore as _explore  # noqa: E402
from . import scan as _scan  # noqa: E402
from . import social as _social  # noqa: E402

app.include_router(_scan.router)
app.include_router(_explore.router)
app.include_router(_auth.router)
app.include_router(_agent.router)
app.include_router(_evidence.router)
app.include_router(_dossier.router)
app.include_router(_social.router)
app.include_router(_dataroom.router)


# 優先序梯階。**這是唯一定義**：`/api/proposal` 靠它決定挑選順序，
# `load_payload` 靠它替每一個點位標上 `tier`。
#
# 為什麼每個點位都要帶 `tier`，而不是只有提案列有：助理點名機構時
# （例如「蘆洲區 20 筆」），前端的清單是拿 id 去**點位表**取列，不是提案表。
# 少了這個欄位，清單樣板會在 `tier.startsWith` 炸掉，而整句
# `innerHTML = rows.map(...)` 於是從未執行——畫面留著上一次的全市提案。
# 表徵是「助理說蘆洲區，清單列的是別區」，而且沒有任何錯誤訊息會浮到使用者
# 眼前。這個誤會實際發生過，也是把梯階從 `get_proposal` 裡搬出來的原因。
TIER_LADDER: tuple[tuple[Callable[[dict], bool], str], ...] = (
    (lambda p: p["ch"] > 0, "財報法遵未通過（高）"),
    (lambda p: p["cf"] > 0, "財報法遵未通過"),
    (lambda p: p.get("ep", 0) > 0, "近兩年評鑑部分指標未通過"),
    (lambda p: p["e90"] > 0, "近 90 日官方事件"),
)


def tier_of(p: dict) -> str:
    """點位落在哪一階。沒踩到任何一階就是「分數排序」——**不是**「低風險」。"""
    for pred, label in TIER_LADDER:
        if pred(p):
            return label
    return "分數排序"


def load_payload(path: pathlib.Path = PAYLOAD_PATH) -> dict[str, Any]:
    """Read the built payload once; fail loudly rather than serving half of it."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} 不存在。請先執行：\n"
            "  PYTHONPATH=src .venv/bin/python scripts/build_frontend.py"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    for p in payload.get("points", []):
        p["tier"] = tier_of(p)
    _state["payload"] = payload
    _state["index"] = {p["i"]: p for p in payload.get("points", [])}
    return payload


def payload() -> dict[str, Any]:
    if _state["payload"] is None:
        load_payload()
    return _state["payload"]




# ── 讀取 ────────────────────────────────────────────────────────────
@app.get("/api/payload")
def get_payload() -> dict[str, Any]:
    return payload()


@app.get("/api/institutions")
def list_institutions(town: Optional[str] = None,
                      type_: Optional[int] = None) -> dict:
    """Map points. Kept lean so a 1,213-marker layer loads fast."""
    pts = payload().get("points", [])
    if town:
        pts = [p for p in pts if p["d"] == town]
    if type_ is not None:
        pts = [p for p in pts if p["t"] == type_]
    return {"count": len(pts), "institutions": pts}


@app.get("/api/districts")
def list_districts() -> dict:
    return {"districts": payload().get("districts", []),
            "boundary": payload().get("boundary", [])}


@app.get("/api/land")
def get_land() -> dict:
    """鄰縣市的陸地輪廓。地圖用它把新北以外的**陸地**反灰，海留原色。

    刻意不進 payload：這是圖磚底圖才有的問題。靜態版自己畫 SVG，畫面上根本沒有
    別的縣市，把 105 KB 內嵌進去只是讓那一份變胖。檔案不在就回空陣列——前端的
    遮罩會自己退回舊做法，地圖照常能用。
    """
    if _state.get("land") is None:
        try:
            _state["land"] = json.loads(LAND_PATH.read_text(encoding="utf-8"))
        except OSError:
            print(f"⚠️ {LAND_PATH} 不存在，地圖反灰會連海一起灰掉。"
                  "重建：python run.py neighbor-land")
            _state["land"] = []
    return {"land": _state["land"]}


@app.get("/api/institutions/{institution_id}")
def get_institution(institution_id: str) -> dict:
    """One 園's full dossier, assembled the same way the static console does."""
    payload()
    p = _state["index"].get(institution_id)
    if not p:
        raise HTTPException(404, f"查無機構 {institution_id}")
    data = payload()
    code = next((c for c, v in data.get("dossier", {}).items()
                 if institution_id in v.get("ids", [])), None)
    return {
        "institution": p,
        "dossier": data.get("dossier", {}).get(code) if code else None,
        "benchmarks": data.get("bench", {}),
        "realtime": {
            "swept_at": data.get("realtime", {}).get("swept_at", ""),
            "channels_live": data.get("realtime", {}).get("channels_live", 0),
            "channels_total": data.get("realtime", {}).get("channels_total", 0),
            "channels": data.get("realtime", {}).get("channels", []),
            "mentions": data.get("realtime", {}).get(
                "by_institution", {}).get(institution_id, []),
        },
        # Repeated on every dossier response on purpose: a caller that only ever
        # reads this endpoint must still be told what the ranking is and is not.
        "disclaimer": "本表為建議查核之優先序，非違法認定；"
                      "無公開財報者屬資料不足，非低風險。",
    }


@app.get("/api/proposal")
def get_proposal(n: int = 20, town: Optional[str] = None,
                 financial_only: bool = False) -> dict:
    """The dispatch proposal, using the same explicit tiers as the console."""
    pts = payload().get("points", [])
    if town:
        pts = [p for p in pts if p["d"] == town]
    if financial_only:
        pts = [p for p in pts if p["fin"]]

    seen: set[str] = set()
    out: list[dict] = []

    def take(pred, why: str) -> None:
        for p in pts:
            if len(out) >= n:
                return
            if p["i"] in seen or not pred(p):
                continue
            seen.add(p["i"])
            out.append({**p, "tier": why})

    for pred, label in TIER_LADDER:
        take(pred, label)
    for p in sorted((p for p in pts if p["i"] not in seen), key=lambda p: p["r"]):
        if len(out) >= n:
            break
        seen.add(p["i"])
        out.append({**p, "tier": "分數排序"})
    return {"requested": n, "returned": len(out), "proposal": out}


@app.get("/api/realtime/{institution_id}")
def realtime_now(institution_id: str) -> dict:
    """Re-run the live channels for one 園, bypassing the build-time snapshot."""
    from ..realtime.monitor import watch

    payload()
    p = _state["index"].get(institution_id)
    if not p:
        raise HTTPException(404, f"查無機構 {institution_id}")
    return watch({"id": institution_id, "title": p["full"], "town": p["d"]})


@app.get("/api/reviews/{institution_id}")
def reviews(institution_id: str) -> dict:
    """Google 評論，開卷宗時即時取用。

    實作搬到 `api/social.py::reviews_of()`，因為社群面板要用同一份結果。
    **回傳形狀完全不變**——搬過去是為了不要有第二份：各寫一份的那天，就是
    卷宗與面板對同一家園講出不同星等的那天。額度閘門與「不是風險訊號」那句
    定位都在那支函式裡。
    """
    return _social.reviews_of(institution_id)


@app.get("/api/config")
def client_config() -> dict:
    """前端啟動時需要知道的設定。

    Maps JavaScript API 金鑰本來就會出現在瀏覽器端——它的保護方式是在 Google
    Console 設 HTTP referrer 限制，不是隱藏金鑰。這裡只送底圖需要的那一把，
    其餘憑證（Apify token 等）一律不外送。
    """
    return {
        "google_maps_key": config.get("GOOGLE_MAPS_API_KEY"),
        "channels_live": (payload().get("realtime") or {}).get("channels_live", 0),
        "channels_total": (payload().get("realtime") or {}).get("channels_total", 0),
    }


@app.get("/api/health")
def health() -> dict:
    """What loaded, what is missing, and which channels are actually watching."""
    try:
        data = payload()
    except FileNotFoundError:
        data = None
    rt = (data or {}).get("realtime", {})
    return {
        "payload_loaded": data is not None,
        "payload_path": str(PAYLOAD_PATH),
        "institutions": len((data or {}).get("points", [])),
        "dossiers": len((data or {}).get("dossier", {})),
        "districts": len((data or {}).get("districts", [])),
        "realtime_swept_at": rt.get("swept_at", ""),
        "channels_live": rt.get("channels_live", 0),
        "channels_total": rt.get("channels_total", 0),
        "schema_version": (data or {}).get("schema_version"),
        # 自然語言查詢現在實際由誰做計畫。決賽當天要能一眼看出「台上跑的是
        # Bedrock 還是關鍵字降級版」，而不是等問了一題才從回應裡發現。
        "chat_planner": _chat_planner_kind(),
        # 缺的是授權不是資料——照實列出，並附上去哪裡申請。
        "credentials": config.status(),
        # `credentials` 只回答「有沒有設」。臨時憑證過期之後變數還在，
        # 所以另外真的驗一次——不然這支端點會在憑證死掉時回報一切正常。
        "aws": config.aws_identity(),
    }


def _chat_planner_kind() -> str:
    """/api/health 用。解析失敗時回報 unknown，不要讓健康檢查自己掛掉。

    `CHAT_PLANNER` 打錯字時要說出來。只顯示退回後的 `keyword` 的話，
    設定錯誤會看起來像「Bedrock 莫名其妙沒被用到」。
    """
    try:
        from .chat import invalid_kind, resolve_kind

        kind = resolve_kind()
        bad = invalid_kind()
        return f"{kind}（CHAT_PLANNER={bad!r} 無法辨識，已退回）" if bad else kind
    except Exception as exc:  # noqa: BLE001 - 健康檢查必須永遠回得了話
        return f"unknown ({type(exc).__name__})"


# ── 聊天 ────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    question: str
    institution_id: Optional[str] = None


#: Planner 實例依種類快取。`BedrockPlanner` 第一次 `plan()` 時才建 client，
#: 每次請求重建等於每次查詢多一次 client 初始化。種類本身每次重新解析
#: （見 `chat.resolve_kind`），所以**啟動時沒憑證、之後才補上 .env** 不必重啟。
#: ⚠️ 但**換掉一把已載入過的金鑰一定要重啟**——理由見 `chat.resolve_kind`
#: 與 `bedrock.client` 的說明（.env 不覆寫、botocore 與 SDK 都有快取）。
_planners: dict[str, Any] = {}


def chat_planner() -> Any:
    from .chat import get_planner, resolve_kind

    kind = resolve_kind()
    if kind not in _planners:
        _planners[kind] = get_planner(kind)
    return _planners[kind]


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    from .chat import answer, get_planner

    try:
        return answer(req.question, payload(),
                      institution_id=req.institution_id, planner=chat_planner())
    except Exception as exc:  # noqa: BLE001 - 降級，不是吞錯：原因照實回報給呼叫端
        # 會場斷網、STS 憑證過期、模型被限流都會走到這裡。示範時讓查詢變慢是
        # 可以接受的，讓它變成 500 不行——KeywordPlanner 產生的是同一組檢索
        # 條件，所以降級只影響「問句怎麼被理解」，不影響答案的措辭與界線。
        from ..bedrock import explain_error

        result = answer(req.question, payload(),
                        institution_id=req.institution_id,
                        planner=get_planner("keyword"))
        result["planner_fallback"] = explain_error(exc)
        return result


if _mcp_app is not None:
    app.mount("/mcp", _mcp_app)


# ── 靜態前端 ─────────────────────────────────────────────────────────
class NoCacheStatic(StaticFiles):
    """前端檔案一律要求瀏覽器回來驗證。

    StaticFiles 只送 ETag 與 Last-Modified，**沒有** Cache-Control，瀏覽器於是
    用啟發式快取自己決定要不要問。結果是改了 app.js、重整卻還是舊畫面——這個
    誤會很貴：看起來像功能沒做出來，實際上是根本沒載到新檔。多一趟 304 的成本
    可以忽略，示範現場的「怎麼沒生效」不行。
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


if WEBAPP.exists():
    app.mount("/static", NoCacheStatic(directory=str(WEBAPP)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(WEBAPP / "index.html"),
                            headers={"Cache-Control": "no-cache"})
