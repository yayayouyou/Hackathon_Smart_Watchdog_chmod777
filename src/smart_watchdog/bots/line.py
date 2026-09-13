"""LINE bot（Guardian_BOT，@727ndzcn）：與 Telegram 同一套內容與閘門，只換傳輸層。

**LINE 只能 webhook。** LINE 伺服器把事件推到我們的公開 HTTPS 網址，所以一定要通道
（見 docs/ENVIRONMENTS.md §6、§8）。本機沒有通道時這支照樣載入、測試照樣過，只是
收不到訊息——不會讓 server 起不來。

與 `telegram.py` 刻意不同的三件事：

1. **驗簽。** `X-Line-Signature`＝以 channel secret 對**原始 body** 做 HMAC-SHA256 的
   base64。驗不過一律 400、事件不處理：webhook 網址是公開的，任何人都能對它 POST
   一個「我是 LINE，某某人要看地圖」。
2. **圖片只能給網址。** LINE 的圖片訊息沒有上傳，只收 HTTPS 網址。地圖因此由
   `/api/bot/map.png` 以**簽章網址**提供（10 分鐘過期、改一個字就 403）——名單上的紅點
   不能變成一張誰都下載得到的圖。沒有 `PUBLIC_BASE_URL` 時只送文字清單，並說明原因。
3. **reply token 大約一分鐘內要用掉。** webhook 先回 200，事件丟背景執行緒處理
   （答詢走 Bedrock 要幾秒；不先回 200，LINE 會判逾時並重送同一個事件）。

一樣的三條：沒綁定沒資料、群組拒絕、只查不做。
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import threading
import time
from typing import Any

import httpx

from . import content, linking

API = "https://api.line.me/v2/bot"
OAUTH = "https://api.line.me/v2/oauth/accessToken"
WEBHOOK_PATH = "/api/bot/line/webhook"
MAP_TTL = 600               # 簽章地圖網址的有效秒數
MAP_CACHE_SECONDS = 120     # LINE 會各抓一次原圖與預覽圖，不必畫兩次

_MAP_CACHE: dict[str, Any] = {"at": 0.0, "png": b""}
_TOKEN: dict[str, str] = {"override": ""}

WELCOME = (
    "我是小小守護員 🐕 新北市教保機構的稽查助理。\n\n"
    "這個帳號只回應已綁定派工台帳號的稽查人員，所以現在還不能提供任何資料。\n\n"
    "綁定方式：傳「綁定 ＋ 綁定碼」給我。綁定碼由派工台產生；展示時由承辦人提供。"
)
PRIVATE_ONLY = "為了避免名單被群組裡沒有登入過派工台的人看到，資料只在一對一聊天中提供。"
HELP = (
    "可以這樣用：\n"
    "• 按下方「地圖＋清單」看本批建議查核的分布\n"
    "• 直接打一句話問，例如：三重區有哪些幼兒園財報法遵沒通過？\n"
    "• 傳「解除綁定」可以解除\n\n"
    "我只查手上的資料、不操作任何畫面，也不會替你送出任何東西。\n"
    + content.DISCLAIMER
)
BAD_CODE = "這組綁定碼無效或已過期。請向承辦人或派工台重新取得。"
NO_PUBLIC_URL = "（地圖需要公開的 HTTPS 網址才傳得出去，目前先給清單。）"


@dataclasses.dataclass
class LineState:
    events_total: int = 0
    refused_unlinked: int = 0
    last_error: str = ""


STATE = LineState()


def _cfg(key: str) -> str:
    from .. import config

    return (config.get(key) or "").strip()


def secret() -> str:
    return _cfg("LINE_CHANNEL_SECRET")


def access_token() -> str:
    return _TOKEN["override"] or _cfg("LINE_CHANNEL_ACCESS_TOKEN")


def configured() -> bool:
    return bool(secret() and (access_token() or _cfg("LINE_CHANNEL_ID")))


def public_base() -> str:
    base = _cfg("PUBLIC_BASE_URL").rstrip("/")
    # LINE 只收 https 的圖片網址；http 給了也是被拒，不如當作沒設。
    return base if base.startswith("https://") else ""


def _scrub(text: str) -> str:
    for value in (access_token(), secret()):
        if value:
            text = text.replace(value, "<redacted>")
    return text


def verify_signature(body: bytes, signature: str) -> bool:
    key = secret()
    if not key or not signature:
        return False
    digest = hmac.new(key.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), signature)


def _refresh_token() -> str:
    """用 Channel ID＋secret 換一組短期 token（30 天）。換不到回空字串。"""
    cid, key = _cfg("LINE_CHANNEL_ID"), secret()
    if not cid or not key:
        return ""
    try:
        r = httpx.post(OAUTH, data={"grant_type": "client_credentials",
                                    "client_id": cid, "client_secret": key}, timeout=20)
        token = (r.json() or {}).get("access_token", "")
    except Exception as exc:  # noqa: BLE001
        STATE.last_error = _scrub(f"換 token 失敗：{type(exc).__name__}: {exc}")
        return ""
    if token:
        _TOKEN["override"] = token
    return token


def call(path: str, payload: dict) -> dict:
    """一次 Messaging API 呼叫。不丟例外；401 時換一次 token 再試；錯誤訊息不含憑證。"""
    for attempt in (1, 2):
        token = access_token() or _refresh_token()
        if not token:
            STATE.last_error = "沒有 LINE access token"
            return {}
        try:
            r = httpx.post(API + path, json=payload, timeout=20,
                           headers={"Authorization": f"Bearer {token}"})
        except Exception as exc:  # noqa: BLE001
            STATE.last_error = _scrub(f"{type(exc).__name__}: {exc}")
            return {}
        if r.status_code == 401 and attempt == 1 and _refresh_token():
            continue
        if r.status_code >= 400:
            STATE.last_error = _scrub(f"HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return {}
    return {}


def reply(reply_token: str, messages: list[dict]) -> None:
    call("/message/reply", {"replyToken": reply_token, "messages": messages[:5]})


def quick_reply() -> dict:
    def item(label: str, data: str, shown: str) -> dict:
        return {"type": "action", "action": {"type": "postback", "label": label,
                                             "data": data, "displayText": shown}}
    return {"items": [item("地圖＋清單", "map", "地圖＋待稽核清單"),
                      item("只看清單", "list", "只看清單"),
                      item("可以問什麼", "help", "可以問什麼")]}


def text(body: str, *, menu: bool = False) -> dict:
    msg: dict[str, Any] = {"type": "text", "text": body[:5000]}
    if menu:
        msg["quickReply"] = quick_reply()
    return msg


# ── 簽章地圖 ─────────────────────────────────────────────────────────


def _map_key() -> bytes:
    key = secret()
    # 與 webhook 驗簽用不同的衍生金鑰：一個簽章不能同時在兩個地方被接受。
    return hashlib.sha256(("map-image:" + key).encode("utf-8")).digest() if key else b""


def _sign(exp: int) -> str:
    mac = hmac.new(_map_key(), str(exp).encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def map_url() -> str:
    exp = int(time.time()) + MAP_TTL
    return f"{public_base()}/api/bot/map.png?exp={exp}&sig={_sign(exp)}"


def check_map_signature(exp: str, sig: str) -> bool:
    if not _map_key():
        return False
    try:
        value = int(exp)
    except (TypeError, ValueError):
        return False
    now = time.time()
    if value < now or value > now + MAP_TTL + 5:
        return False
    return hmac.compare_digest(_sign(value), str(sig or ""))


def map_png() -> bytes:
    if _MAP_CACHE["png"] and time.time() - _MAP_CACHE["at"] < MAP_CACHE_SECONDS:
        return _MAP_CACHE["png"]
    from ..api import server
    from . import mapimage

    data = server.payload()
    png = mapimage.render(data.get("points") or [], data.get("boundary") or [],
                          content.proposal(20), as_of=content.as_of())
    _MAP_CACHE.update(at=time.time(), png=png)
    return png


# ── 事件 ─────────────────────────────────────────────────────────────

_WORDS = {"地圖": "map", "地圖＋清單": "map", "地圖＋待稽核清單": "map", "/map": "map",
          "清單": "list", "只看清單": "list", "/list": "list",
          "說明": "help", "可以問什麼": "help", "help": "help", "/help": "help",
          "解除綁定": "unbind", "/unbind": "unbind"}


def _parse(raw: str) -> tuple[str, str]:
    for prefix in ("綁定", "/start", "/bind"):
        if raw.startswith(prefix):
            return "bind", raw[len(prefix):].strip()
    if raw in _WORDS:
        return _WORDS[raw], ""
    if raw.startswith("/ask"):
        return "ask", raw[4:].strip()
    return "ask", raw


def _demo_link(db, arg: str, user_id: str):
    expected = _cfg("LINE_DEMO_PASSCODE") or _cfg("TELEGRAM_DEMO_PASSCODE")
    if not expected or not arg or not hmac.compare_digest(arg.encode(), expected.encode()):
        return None
    user = linking.demo_user(db, _cfg("SEED_INSPECTOR_EMAIL") or None)
    return linking.link(db, user, "line", user_id) if user is not None else None


def handle_event(event: dict) -> None:
    from ..db import session as dbsession

    STATE.events_total += 1
    token = event.get("replyToken")
    source = event.get("source") or {}
    if not token:
        return
    if source.get("type") != "user":
        reply(token, [text(PRIVATE_ONLY)])
        return
    user_id = str(source.get("userId") or "")
    kind = event.get("type")
    if kind == "follow":
        reply(token, [text(WELCOME)])
        return
    if kind == "postback":
        cmd, arg = str((event.get("postback") or {}).get("data") or ""), ""
    elif kind == "message" and (event.get("message") or {}).get("type") == "text":
        cmd, arg = _parse(str(event["message"].get("text") or "").strip())
    else:
        return

    db = dbsession.session()
    try:
        if cmd == "bind":
            user = linking.redeem(db, arg, "line", user_id) or _demo_link(db, arg, user_id)
            if user is None:
                reply(token, [text(BAD_CODE)])
                return
            print(f"[line] 綁定：{user.email} ↔ {user_id[:8]}…")
            reply(token, [text(f"綁定完成：{user.name}。從現在起這個聊天可以查資料。", menu=True)])
            return

        user = linking.user_for(db, "line", user_id)
        if user is None:
            STATE.refused_unlinked += 1
            reply(token, [text(WELCOME)])
            return

        if cmd == "map":
            print(f"[line] {user.email} 查地圖")
            messages: list[dict] = []
            if public_base():
                url = map_url()
                messages.append({"type": "image", "originalContentUrl": url, "previewImageUrl": url})
            else:
                messages.append(text(NO_PUBLIC_URL))
            messages.append(text(content.proposal_text(n=20), menu=True))
            reply(token, messages)
        elif cmd == "list":
            reply(token, [text(content.proposal_text(n=20), menu=True)])
        elif cmd == "unbind":
            linking.unlink(db, "line", user_id)
            reply(token, [text("已解除綁定。這個聊天不會再收到任何資料。")])
        elif cmd == "ask" and arg:
            print(f"[line] {user.email} 答詢")
            reply(token, [text(content.answer_text(arg), menu=True)])
        else:
            reply(token, [text(HELP, menu=True)])
    finally:
        db.close()


def _run(events: list[dict]) -> None:
    for event in events:
        try:
            handle_event(event)
        except Exception as exc:  # noqa: BLE001 - 一個事件失敗不能拖垮其他事件
            STATE.last_error = _scrub(f"處理事件失敗：{type(exc).__name__}: {exc}")[:300]


def dispatch_async(events: list[dict]) -> None:
    threading.Thread(target=_run, args=(events,), name="line-events", daemon=True).start()


def status() -> dict[str, Any]:
    return {"configured": configured(), "webhook_path": WEBHOOK_PATH,
            "public_base_url": public_base(), "map_images": bool(public_base()),
            **dataclasses.asdict(STATE)}
