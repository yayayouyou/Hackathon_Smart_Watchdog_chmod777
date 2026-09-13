"""Telegram bot（t.me/Little_Guardian_bot）：手機上看地圖、待稽核清單、一句話答詢。

**傳輸用 long polling，不用 webhook。** 與 MaiCoin 那版同一個理由：不需要公開網址，
本機 `run.py serve` 起來就能用，會場沒有 HTTPS 通道也不影響。代價是**一個 token
同一時間只能有一個行程在輪詢**——第二個會拿到 409。所以：

* `TELEGRAM_BOT_POLLING=false` 可以關掉（跑測試 server、或另一台機器已經在跑時）。
* 409 會照實寫進 `last_error`，而不是安靜地重試到天荒地老。

**與 MaiCoin 那版刻意不同的三件事**（理由見 `bots/__init__.py`）：

1. 每一個會回資料的指令都先過 `linking.user_for()`。未綁定的聊天室只會收到
   「怎麼綁定」，連「名單有幾家」都不給。
2. 群組一律拒絕，**連綁定都不做**。群組裡的其他人沒有登入過派工台。
3. 不接 agent。答詢是 `chat.answer()`，只查、不做。

**示範暗號（`TELEGRAM_DEMO_PASSCODE`）。** 決賽展示時這個 bot 是小彩蛋，走完整的
「派工台登入 → 產生一次性碼」太繞。所以另開一條：`t.me/<bot>?start=<暗號>`，點開按
「開始」就綁到示範稽查員帳號。它**保留了門**——沒有暗號的路人照樣拿不到名單——但
暗號可以重複使用，**展示結束請把它從 .env 拿掉或換掉**。沒設暗號時這條路不存在。

**token 不得出現在任何錯誤訊息裡。** httpx 的例外字串可能帶著請求網址，而網址裡
就是 token；`last_error` 會出現在 `/api/health`。`call()` 回傳前一律把它遮掉。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hmac
import json
import threading
from typing import Any

import httpx

from . import content, linking

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30            # getUpdates 的伺服器端等待秒數
LIST_SIZE = 20

_STARTED = False
_LOCK = threading.Lock()

UNLINKED = (
    "我是小小守護員 🐕 新北市教保機構的稽查助理。\n\n"
    "這個 bot 只回應已綁定派工台帳號的稽查人員，所以現在還不能提供任何資料。\n\n"
    "綁定方式：登入派工台 → 右上角身分卡 →「綁定 Telegram」→ 點開那個連結並按「開始」。"
)
PRIVATE_ONLY = "為了避免名單被群組裡沒有登入過派工台的人看到，資料只在私訊中提供。請直接私訊我。"
HELP = (
    "可以這樣用：\n"
    "• 按「地圖＋待稽核清單」看本批建議查核的分布\n"
    "• 直接打一句話問，例如：三重區有哪些幼兒園財報法遵沒通過？\n"
    "• /unbind 解除這個對話的綁定\n\n"
    "我只查手上的資料、不操作任何畫面，也不會替你送出任何東西。\n"
    + content.DISCLAIMER
)
BAD_CODE = "這組綁定碼無效或已過期（10 分鐘內有效、只能用一次）。請回派工台重新產生。"


@dataclasses.dataclass
class BotState:
    """給 `/api/health` 照實顯示。"""

    enabled: bool = False
    reason: str = ""
    username: str = ""
    cycles: int = 0
    last_run_utc: str = ""
    last_error: str = ""
    updates_total: int = 0
    refused_unlinked: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


STATE = BotState()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token() -> str | None:
    from .. import config

    return (config.get("TELEGRAM_BOT_TOKEN") or "").strip() or None


def _flag(key: str, default: bool) -> bool:
    from .. import config

    raw = (config.get(key) or "").strip().lower()
    return default if not raw else raw not in ("0", "false", "off", "no")


def call(method: str, tok: str, *, files: dict | None = None,
         http_timeout: float = 20, **params: Any) -> dict:
    """一次 Bot API 呼叫。**不丟例外**，而且回傳內容裡不會有 token。"""
    url = API.format(token=tok, method=method)
    try:
        if files:
            # multipart 時，巢狀參數（reply_markup）要自己序列化成 JSON 字串。
            data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
                    for k, v in params.items()}
            resp = httpx.post(url, data=data, files=files, timeout=http_timeout)
        else:
            resp = httpx.post(url, json=params, timeout=http_timeout)
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - 網路的每一種失敗都只該讓這一次沒結果
        return {"ok": False, "description": f"{type(exc).__name__}: {exc}".replace(tok, "<token>")}


def menu() -> dict:
    return {"inline_keyboard": [
        [{"text": "🗺 地圖＋待稽核清單", "callback_data": "map"}],
        [{"text": "📋 只看清單", "callback_data": "list"},
         {"text": "❓ 可以問什麼", "callback_data": "help"}],
    ]}


def send(chat_id: str, text: str, tok: str, reply_markup: dict | None = None) -> None:
    parts = content.chunks(text)
    for i, part in enumerate(parts):
        params: dict[str, Any] = {"chat_id": chat_id, "text": part}
        if reply_markup is not None and i == len(parts) - 1:
            params["reply_markup"] = reply_markup
        call("sendMessage", tok, **params)


def send_map(chat_id: str, tok: str) -> None:
    from ..api import server
    from . import mapimage

    call("sendChatAction", tok, chat_id=chat_id, action="upload_photo")
    data = server.payload()
    rows = content.proposal(LIST_SIZE)
    png = mapimage.render(data.get("points") or [], data.get("boundary") or [], rows,
                          as_of=content.as_of())
    call("sendPhoto", tok, files={"photo": ("map.png", png, "image/png")}, http_timeout=60,
         chat_id=chat_id,
         caption=f"本批建議查核 {len(rows)} 家的分布，紅點編號＝下方清單序號。")
    send(chat_id, content.proposal_text(rows), tok, reply_markup=menu())


def _parse(text: str, from_button: bool) -> tuple[str, str]:
    if from_button:
        return text, ""
    if text.startswith("/"):
        head, _, arg = text.partition(" ")
        return head[1:].split("@", 1)[0].lower(), arg.strip()
    return "ask", text


def _demo_link(db, arg: str, chat_id: str):
    """示範暗號。沒設就不存在；比對用 compare_digest，不讓回應時間洩漏暗號。"""
    from .. import config

    expected = (config.get("TELEGRAM_DEMO_PASSCODE") or "").strip()
    if not expected or not hmac.compare_digest(arg.encode(), expected.encode()):
        return None
    user = linking.demo_user(db, config.get("SEED_INSPECTOR_EMAIL"))
    return linking.link(db, user, "telegram", chat_id) if user is not None else None


def _dispatch(chat: dict, text: str, tok: str, *, from_button: bool) -> None:
    from ..db import session as dbsession

    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return
    if chat.get("type") != "private":
        send(chat_id, PRIVATE_ONLY, tok)
        return

    cmd, arg = _parse(text, from_button)
    db = dbsession.session()
    try:
        if cmd in ("start", "bind") and arg:
            user = (linking.redeem(db, arg, "telegram", chat_id)
                    or _demo_link(db, arg, chat_id))
            if user is None:
                send(chat_id, BAD_CODE, tok)
                return
            print(f"[telegram] 綁定：{user.email} ↔ chat {chat_id}")
            send(chat_id, f"綁定完成：{user.name}（{user.email}）。\n從現在起這個對話可以查資料。",
                 tok, reply_markup=menu())
            return

        user = linking.user_for(db, "telegram", chat_id)
        if user is None:
            STATE.refused_unlinked += 1
            send(chat_id, UNLINKED, tok)
            return

        if cmd == "start":
            send(chat_id, f"{user.name} 您好，要看什麼？", tok, reply_markup=menu())
        elif cmd == "help":
            send(chat_id, HELP, tok, reply_markup=menu())
        elif cmd == "unbind":
            linking.unlink(db, "telegram", chat_id)
            send(chat_id, "已解除綁定。這個對話不會再收到任何資料。", tok)
        elif cmd == "map":
            print(f"[telegram] {user.email} 查地圖")
            send_map(chat_id, tok)
        elif cmd == "list":
            print(f"[telegram] {user.email} 查清單")
            send(chat_id, content.proposal_text(n=LIST_SIZE), tok, reply_markup=menu())
        elif cmd == "ask":
            if not arg:
                send(chat_id, "請在 /ask 後面接一句問題，或直接打字問我。", tok)
                return
            print(f"[telegram] {user.email} 答詢")
            call("sendChatAction", tok, chat_id=chat_id, action="typing")
            send(chat_id, content.answer_text(arg), tok, reply_markup=menu())
        else:
            send(chat_id, HELP, tok, reply_markup=menu())
    finally:
        db.close()


def handle_update(update: dict, tok: str) -> None:
    STATE.updates_total += 1
    if isinstance(update.get("callback_query"), dict):
        cq = update["callback_query"]
        call("answerCallbackQuery", tok, callback_query_id=cq.get("id"))
        chat = (cq.get("message") or {}).get("chat") or {}
        _dispatch(chat, str(cq.get("data") or "").strip(), tok, from_button=True)
        return
    msg = update.get("message") or update.get("edited_message")
    if not isinstance(msg, dict):
        return
    text = str(msg.get("text") or "").strip()
    if text:
        _dispatch(msg.get("chat") or {}, text, tok, from_button=False)


COMMANDS = [
    {"command": "map", "description": "地圖＋待稽核清單"},
    {"command": "list", "description": "待稽核清單"},
    {"command": "ask", "description": "問一句話（也可以直接打字）"},
    {"command": "unbind", "description": "解除這個對話的綁定"},
    {"command": "help", "description": "說明"},
]


def _loop(stop: threading.Event, tok: str) -> None:
    me = call("getMe", tok)
    if me.get("ok"):
        STATE.username = me["result"].get("username", "") or STATE.username
    # 殘留的 webhook 會讓 getUpdates 回 409；清掉它不會丟訊息。
    call("deleteWebhook", tok, drop_pending_updates=False)
    call("setMyCommands", tok, commands=COMMANDS)

    offset: int | None = None
    while not stop.is_set():
        params: dict[str, Any] = {"timeout": POLL_TIMEOUT,
                                  "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        data = call("getUpdates", tok, http_timeout=POLL_TIMEOUT + 15, **params)
        STATE.cycles += 1
        STATE.last_run_utc = _now()
        if not data.get("ok"):
            desc = str(data.get("description") or "")
            if data.get("error_code") == 409 or "Conflict" in desc:
                STATE.last_error = ("409：另一個行程也在用這個 token 輪詢。"
                                    "同一時間只能一個；其他地方請設 TELEGRAM_BOT_POLLING=false")
                stop.wait(30)
            else:
                STATE.last_error = desc[:300]
                stop.wait(5)
            continue
        STATE.last_error = ""
        for upd in data.get("result") or []:
            offset = int(upd["update_id"]) + 1
            try:
                handle_update(upd, tok)
            except Exception as exc:  # noqa: BLE001 - 一則訊息處理失敗不能讓 bot 停掉
                STATE.last_error = f"處理訊息失敗：{type(exc).__name__}: {exc}"[:300]


def start() -> BotState:
    """啟動 long polling。重複呼叫安全；沒有 token 或被關掉時說出原因。"""
    global _STARTED
    from .. import config

    with _LOCK:
        if _STARTED:
            return STATE
        STATE.username = (config.get("TELEGRAM_BOT_USERNAME") or "").lstrip("@")
        if not _flag("TELEGRAM_BOT_POLLING", True):
            STATE.enabled, STATE.reason = False, "已由 TELEGRAM_BOT_POLLING 關閉"
            return STATE
        tok = token()
        if not tok:
            STATE.enabled, STATE.reason = False, "TELEGRAM_BOT_TOKEN 未設定"
            return STATE
        STATE.enabled, STATE.reason = True, ""
        threading.Thread(target=_loop, args=(threading.Event(), tok),
                         name="telegram-bot", daemon=True).start()
        _STARTED = True
        print(f"[telegram] bot 已啟動（long polling）：t.me/{STATE.username or '?'}")
        return STATE


def status() -> dict[str, Any]:
    return STATE.as_dict()
