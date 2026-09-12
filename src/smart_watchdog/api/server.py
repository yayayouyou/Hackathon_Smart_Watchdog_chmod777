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
    POST /api/chat               自然語言查詢
    GET  /api/timeline           時間軸回測：每年重訓一次的實測成績
    GET  /api/timeline/{as_of}   某一格的逐園排序與事後命中
    GET  /api/docsearch?q=       文件索引：問題進、檔案與頁碼出
    GET  /api/health             各資料表與管道的就緒狀態

資料在啟動時載入一次進記憶體。全市 1,213 園的 payload 約 900 KB，重載成本
遠低於每次請求重算，而且讓 /api/health 能誠實回報「載入了什麼、少了什麼」。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config

ROOT = pathlib.Path(__file__).resolve().parents[3]
WEBAPP = ROOT / "webapp"
PAYLOAD_PATH = ROOT / "dist/data/payload.json"

app = FastAPI(
    title="小小守護員 Smart Watchdog",
    description="新北市教保機構稽查優先序。輸出為建議查核，非違法認定。",
    version="1.0",
)
# The console may be served from a different origin during development.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_state: dict[str, Any] = {"payload": None, "index": {}}

# 掃描主控台與證據端點。在此掛載而非讓子模組匯入 server，避免循環匯入。
from . import agent as _agent  # noqa: E402
from . import auth as _auth  # noqa: E402
from . import evidence as _evidence  # noqa: E402
from . import explore as _explore  # noqa: E402
from . import scan as _scan  # noqa: E402

app.include_router(_scan.router)
app.include_router(_explore.router)
app.include_router(_auth.router)
app.include_router(_agent.router)
app.include_router(_evidence.router)


def load_payload(path: pathlib.Path = PAYLOAD_PATH) -> dict[str, Any]:
    """Read the built payload once; fail loudly rather than serving half of it."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} 不存在。請先執行：\n"
            "  PYTHONPATH=src .venv/bin/python scripts/build_frontend.py"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    _state["payload"] = payload
    _state["index"] = {p["i"]: p for p in payload.get("points", [])}
    return payload


def payload() -> dict[str, Any]:
    if _state["payload"] is None:
        load_payload()
    return _state["payload"]


@app.on_event("startup")
def _startup() -> None:
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

    take(lambda p: p["ch"] > 0, "財報法遵未通過（高）")
    take(lambda p: p["cf"] > 0, "財報法遵未通過")
    take(lambda p: p.get("ep", 0) > 0, "近兩年評鑑部分指標未通過")
    take(lambda p: p["e90"] > 0, "近 90 日官方事件")
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

    量測結果決定了它的定位：裁罰 ≥5 件的園評分中位 4.20、無裁罰者 4.60
    （p=0.061，不顯著），而個案完全不具鑑別力——16 件裁罰的幼苗國際 4.7 星、
    13 件的南蒂亞 4.9 星、因虐童停招的吉尼爾 4.4 星。**不是風險訊號**，
    是稽查員到場前值得看一眼的家長觀感。
    """
    import csv

    payload()
    key = config.get("GOOGLE_MAPS_API_KEY")
    p = _state["index"].get(institution_id)
    if not p:
        raise HTTPException(404, f"查無機構 {institution_id}")
    if not key:
        return {"available": False, "reason": "未設定 GOOGLE_MAPS_API_KEY",
                "reviews": []}

    place_id = ""
    table = ROOT / "data/processed/place_ids_ntpc.csv"
    if table.exists():
        with table.open(encoding="utf-8") as fh:
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
        "note": "家長主觀評價，非法遵指標。實測：裁罰 ≥5 件的園評分中位 4.20，"
                "無裁罰者 4.60（p=0.061 不顯著），個案不具鑑別力。",
        "free_remaining": max(
            0, 1000 - _ledger.budget().places_used_this_month),
        "meter_source": "本機計數，非 Google 帳單",
    }


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
        # 缺的是授權不是資料——照實列出，並附上去哪裡申請。
        "credentials": config.status(),
    }


# ── 聊天 ────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    question: str
    institution_id: Optional[str] = None


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    from .chat import answer

    return answer(req.question, payload(), institution_id=req.institution_id)


# ── MCP ──────────────────────────────────────────────────────────────
# 同一組 tool 的第二條入口：瀏覽器走 /api/agent/messages，MCP 客戶端走 /mcp。
# 白名單與稽核都在 ToolRegistry.execute()，所以兩條路徑不可能有不同的權限。
# 掛載失敗不讓整個服務起不來——MCP 是額外通道，派工台本身不依賴它。
try:
    from ..agent.mcp_server import build_mcp as _build_mcp

    app.mount("/mcp", _build_mcp().http_app(path="/"))
except Exception as _mcp_exc:  # noqa: BLE001 - 缺 fastmcp 或版本不符都只停用這條
    print(f"MCP 未掛載：{_mcp_exc}")


# ── 靜態前端 ─────────────────────────────────────────────────────────
if WEBAPP.exists():
    app.mount("/static", StaticFiles(directory=str(WEBAPP)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(WEBAPP / "index.html"))
