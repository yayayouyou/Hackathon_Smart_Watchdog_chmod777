"""派工台這一端的 bot 路由：產生綁定碼、查綁定狀態。

綁定的起點只在這裡——一個已登入的使用者。bot 端沒有建立綁定的路徑，
理由見 `bots/linking.py`。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..bots import line, linking, telegram
from ..db.models import User
from ..db.session import get_db
from .auth import get_current_user

router = APIRouter(prefix="/api/bot", tags=["bot"])


@router.post("/telegram/bind-code")
def telegram_bind_code(user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)) -> dict:
    """一次性綁定碼＋深層連結。手機上點連結、按「開始」就完成綁定。"""
    if not telegram.token():
        raise HTTPException(503, "Telegram bot 尚未設定（缺 TELEGRAM_BOT_TOKEN）")
    code = linking.issue_code(db, user.id, "telegram")
    username = telegram.STATE.username
    return {
        "code": code,
        "expires_in": int(linking.CODE_TTL.total_seconds()),
        "bot": username,
        "deep_link": f"https://t.me/{username}?start={code}" if username else "",
        "polling": telegram.STATE.enabled,
        "note": "10 分鐘內有效、只能用一次。綁定後這個 Telegram 對話可以查地圖、清單與答詢；"
                "帳號停用或在 bot 輸入 /unbind 即失效。",
    }


@router.get("/telegram/status")
def telegram_status(user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> dict:
    return {**telegram.status(), "linked": linking.is_linked(db, user.id, "telegram")}


# ── LINE ─────────────────────────────────────────────────────────────


@router.post("/line/webhook", include_in_schema=False)
async def line_webhook(request: Request) -> Response:
    """LINE Messaging API webhook。驗簽不過 400；通過就先回 200，事件丟背景處理。

    沒設定 LINE 憑證時安靜回 200：LINE 後台的「驗證」按鈕只看 200，回錯誤碼只會
    讓後台一直顯示紅字，而那不是「有人在打我們」的訊號。
    """
    body = await request.body()
    if not line.configured():
        return Response(status_code=200)
    if not line.verify_signature(body, request.headers.get("X-Line-Signature", "")):
        raise HTTPException(400, "invalid signature")
    try:
        events = json.loads(body.decode("utf-8")).get("events") or []
    except ValueError:
        events = []
    if events:
        line.dispatch_async(events)
    return Response(status_code=200)


@router.get("/map.png", include_in_schema=False)
def signed_map(exp: str = "", sig: str = "") -> Response:
    """聊天室用的地圖。**只認簽章網址**（10 分鐘過期），不需要登入——LINE 的伺服器
    要能直接抓——但也因此不能是一個誰都猜得到的固定網址。"""
    if not line.check_map_signature(exp, sig):
        raise HTTPException(403, "連結無效或已過期")
    return Response(line.map_png(), media_type="image/png",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/line/status")
def line_status(user: User = Depends(get_current_user)) -> dict:
    return line.status()

